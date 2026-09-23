"""Black-box acceptance contract for the versioned run-context authority.

Uses real Git linked worktrees, child processes, and SQLite stores; it does
not inspect implementation tables or mock liveness behavior.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import selectors
import subprocess
import sys
import textwrap
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))
INHERITED_CONTEXT_KEYS = (
    "FFS_RUN_ID", "GSD_RUN_ID", "GSD_RESUME", "GSD_WORKSTREAM",
    "GSD_PROJECT", "GSD_SESSION_KEY",
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_text(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _isolated_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in INHERITED_CONTEXT_KEYS:
        env.pop(key, None)
    env["PYTHONPATH"] = f"{LIB}:{env.get('PYTHONPATH', '')}"
    return env


@pytest.fixture
def linked_checkouts(tmp_path: Path) -> tuple[Path, Path]:
    """Two actual linked worktrees belonging to one disposable repository."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", cwd=primary)
    _git("config", "user.email", "acceptance@example.test", cwd=primary)
    _git("config", "user.name", "Acceptance", cwd=primary)
    (primary / "README.md").write_text("fixture\n", encoding="utf-8")
    _git("add", "README.md", cwd=primary)
    _git("commit", "-qm", "fixture", cwd=primary)
    first, second = tmp_path / "linked-a", tmp_path / "linked-b"
    _git("worktree", "add", "-q", "-b", "linked-a", str(first), cwd=primary)
    _git("worktree", "add", "-q", "-b", "linked-b", str(second), cwd=primary)
    return first, second


def _cli(state_root: Path, *args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = _isolated_env()
    if env:
        command_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "run_state.cli", *args, "--state-root", str(state_root)],
        cwd=cwd, env=command_env, capture_output=True, text=True,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def _start(state_root: Path, cwd: Path, objective: str, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _cli(state_root, "start", "--skill", "fix", "--objective", objective,
                "--activity", "plan", "--json", *extra, cwd=cwd, env=env)


def test_repository_registration_admin_is_idempotent_and_creates_no_run(
    linked_checkouts, tmp_path: Path,
) -> None:
    first, second = linked_checkouts
    state_root = tmp_path / "authority"

    registered = _cli(state_root, "register-repository", cwd=first)
    assert registered.returncode == 0, registered.stderr
    first_payload = _payload(registered)
    assert first_payload == {
        "ok": True,
        "repository_id": first_payload["repository_id"],
        "schema_version": 1,
        "state_root": str(state_root.resolve()),
    }
    assert uuid.UUID(first_payload["repository_id"]).version == 4

    repeated = _cli(state_root, "register-repository", cwd=second)
    assert repeated.returncode == 0, repeated.stderr
    assert _payload(repeated) == first_payload

    from run_state.state import ControlStore

    store = ControlStore(state_root / "control.sqlite3")
    with store.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM context_repositories").fetchone()[0] == 1
        assert tx.execute("SELECT COUNT(*) FROM context_runs").fetchone()[0] == 0
        assert tx.execute(
            "SELECT COUNT(*) FROM control_reservations WHERE held=1"
        ).fetchone()[0] == 0
        assert tx.execute(
            "SELECT COUNT(*) FROM control_events WHERE event_type='resources_reserved'"
        ).fetchone()[0] == 0
        assert tx.execute(
            "SELECT value FROM control_generation WHERE singleton=1"
        ).fetchone()[0] == 0
        tables = {row[0] for row in tx.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "authority_launch_intents" not in tables


def test_repository_registration_preserves_and_privates_legacy_ffs_directory(
    linked_checkouts, tmp_path: Path,
) -> None:
    first, _second = linked_checkouts
    common = Path(_git_text("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=first))
    legacy = common / "ffs"
    legacy.mkdir(mode=0o755)
    legacy.chmod(0o755)
    retained = legacy / "gsd-run"
    retained.mkdir()
    sentinel = retained / "legacy-state"
    sentinel.write_text("preserve\n", encoding="utf-8")

    registered = _cli(tmp_path / "authority", "register-repository", cwd=first)
    assert registered.returncode == 0, registered.stderr
    assert (legacy.stat().st_mode & 0o777) == 0o700
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_repository_registration_refuses_symlinked_ffs_directory(
    linked_checkouts, tmp_path: Path,
) -> None:
    first, _second = linked_checkouts
    common = Path(_git_text("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=first))
    outside = tmp_path / "outside-ffs"
    outside.mkdir(mode=0o700)
    (common / "ffs").symlink_to(outside, target_is_directory=True)

    registered = _cli(tmp_path / "authority", "register-repository", cwd=first)
    assert registered.returncode != 0
    assert "REPOSITORY_IDENTITY_INVALID" in registered.stdout + registered.stderr


def test_repository_registration_refuses_symlinked_or_permissive_marker(
    linked_checkouts, tmp_path: Path,
) -> None:
    first, _second = linked_checkouts
    common = Path(_git_text("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=first))
    owned = common / "ffs"
    owned.mkdir(mode=0o700)
    outside = tmp_path / "repository.json"
    outside.write_text(
        json.dumps({"schema_version": 1, "repository_id": str(uuid.uuid4())}),
        encoding="utf-8",
    )
    (owned / "repository.json").symlink_to(outside)

    symlinked = _cli(tmp_path / "symlink-authority", "register-repository", cwd=first)
    assert symlinked.returncode != 0
    assert "REPOSITORY_IDENTITY_INVALID" in symlinked.stdout + symlinked.stderr

    (owned / "repository.json").unlink()
    marker = owned / "repository.json"
    marker.write_text(
        json.dumps({"schema_version": 1, "repository_id": str(uuid.uuid4())}),
        encoding="utf-8",
    )
    marker.chmod(0o644)
    permissive = _cli(tmp_path / "permissive-authority", "register-repository", cwd=first)
    assert permissive.returncode != 0
    assert "REPOSITORY_IDENTITY_INVALID" in permissive.stdout + permissive.stderr


def test_explicit_run_is_durable_across_real_linked_worktrees(linked_checkouts, tmp_path: Path) -> None:
    first, second = linked_checkouts
    state_root = tmp_path / "authority"
    sentinel = first / ".planning" / "acceptance-sentinel"
    sentinel.parent.mkdir()
    sentinel.write_text("unchanged\n")
    first_status = _git_text("status", "--porcelain=v1", cwd=first)
    common = Path(_git_text("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=first))
    base = _git_text("rev-parse", "HEAD", cwd=first)
    started = _start(state_root, first, "durable identity", "--run-id", "spec-014")
    assert started.returncode == 0, started.stderr
    ready = _payload(started)
    assert ready["schema_version"] == 1
    assert ready["run_id"] == "spec-014"
    assert Path(ready["workspace"]).is_absolute()
    assert Path(ready["evidence_root"]).is_absolute()
    assert state_root in Path(ready["evidence_root"]).parents
    workspace = Path(ready["workspace"])
    assert workspace not in (first, second)
    assert _git_text("branch", "--show-current", cwd=workspace) == "ffs/runs/spec-014"
    assert Path(_git_text("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=workspace)) == common
    assert _git_text("rev-parse", "HEAD", cwd=workspace) == base
    assert f"worktree {workspace}" in _git_text("worktree", "list", "--porcelain", cwd=first)
    assert not (workspace / ".planning" / "acceptance-sentinel").exists()
    assert sentinel.read_text() == "unchanged\n"
    assert _git_text("status", "--porcelain=v1", cwd=first) == first_status
    assert ready["attempt_id"] is None
    assert ready["runtime_tuple_hash"] is None
    inspected = _cli(state_root, "context", "--run-id", "spec-014", "--json", cwd=second)
    assert inspected.returncode == 0, inspected.stderr
    context = _payload(inspected)
    assert context["run_id"] == ready["run_id"]
    assert context["repository_id"] == ready["repository_id"]
    assert context["evidence_root"] == ready["evidence_root"]


def test_anonymous_starts_mint_full_uuid_ids_without_collision(linked_checkouts, tmp_path: Path) -> None:
    checkout, _ = linked_checkouts
    one = _payload(_start(tmp_path / "authority", checkout, "anonymous one"))
    two = _payload(_start(tmp_path / "authority", checkout, "anonymous two"))
    assert one["run_id"].startswith("adhoc-")
    assert two["run_id"].startswith("adhoc-")
    assert one["run_id"] != two["run_id"]
    assert one["workspace"] != two["workspace"]
    assert _git_text("branch", "--show-current", cwd=Path(one["workspace"])) != _git_text(
        "branch", "--show-current", cwd=Path(two["workspace"])
    )
    assert len(one["run_id"]) == len("adhoc-") + 32
    assert uuid.UUID(hex=one["run_id"].removeprefix("adhoc-"))


def test_explicit_and_inherited_ids_that_disagree_fail_closed(linked_checkouts, tmp_path: Path) -> None:
    checkout, _ = linked_checkouts
    result = _start(tmp_path / "authority", checkout, "conflict", "--run-id", "explicit-014",
                    env={"GSD_RUN_ID": "inherited-014"})
    assert result.returncode == 2
    refusal = _payload(result)
    assert refusal["ok"] is False
    assert refusal["code"] == "CONFLICTING_RUN_ID"
    assert refusal["recovery_action"]


def test_conflicting_inherited_gsd_and_ffs_aliases_fail_without_store_creation(
    linked_checkouts, tmp_path: Path,
) -> None:
    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    result = _start(
        state_root, checkout, "alias conflict",
        env={"GSD_RUN_ID": "gsd-run", "FFS_RUN_ID": "ffs-run"},
    )
    assert result.returncode == 2
    refusal = _payload(result)
    assert refusal["code"] == "CONFLICTING_RUN_ID"
    assert isinstance(refusal["recovery_action"], dict)
    assert not state_root.exists()


@pytest.mark.parametrize("bad_id", ["x" * 65, "has space"])
def test_invalid_or_overlong_ids_are_rejected_without_truncation(linked_checkouts, tmp_path: Path, bad_id: str) -> None:
    checkout, _ = linked_checkouts
    result = _start(tmp_path / "authority", checkout, "bad id", "--run-id", bad_id)
    assert result.returncode == 2
    refusal = _payload(result)
    assert refusal["code"] == "INVALID_RUN_ID"
    assert bad_id not in refusal.get("run_id", "")


def test_unfinished_activity_requires_explicit_resume_and_env_is_compatible(linked_checkouts, tmp_path: Path) -> None:
    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    first = _start(state_root, checkout, "resume contract", "--run-id", "resume-014")
    assert first.returncode == 0, first.stderr
    rejected = _cli(state_root, "start", "--skill", "fix", "--objective", "resume contract",
                    "--activity", "plan", "--json", cwd=checkout, env={"GSD_RUN_ID": "resume-014"})
    assert rejected.returncode == 3
    assert _payload(rejected)["code"] == "RESUME_REQUIRED"
    resumed = _cli(state_root, "start", "--skill", "fix", "--objective", "resume contract",
                   "--activity", "plan", "--json", "--resume", cwd=checkout,
                   env={"GSD_RUN_ID": "resume-014", "GSD_RESUME": "1"})
    assert resumed.returncode == 0, resumed.stderr
    assert _payload(resumed)["activity_id"] == _payload(first)["activity_id"]


def test_ambiguous_resume_lists_candidates_and_starts_nothing(linked_checkouts, tmp_path: Path) -> None:
    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    assert _start(state_root, checkout, "select me", "--run-id", "candidate-a", "--scope", "api").returncode == 0
    assert _start(state_root, checkout, "select me", "--run-id", "candidate-b", "--scope", "ui").returncode == 0
    result = _cli(state_root, "context", "--objective", "select me", "--resume", "--json", cwd=checkout)
    assert result.returncode == 3
    payload = _payload(result)
    assert payload["code"] == "AMBIGUOUS_RUN"
    assert {candidate["run_id"] for candidate in payload["candidates"]} == {"candidate-a", "candidate-b"}
    assert isinstance(payload["recovery_action"], dict)
    assert payload["recovery_action"]["action"] == "select_run"
    assert "nonce" not in json.dumps(payload).lower()


def test_missing_registered_workspace_remains_inspectable_not_ready(linked_checkouts, tmp_path: Path) -> None:
    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    started = _start(state_root, checkout, "missing workspace", "--run-id", "missing-workspace")
    assert started.returncode == 0, started.stderr
    workspace = Path(_payload(started)["workspace"])
    _git("worktree", "remove", "--force", str(workspace), cwd=checkout)
    inspected = _cli(state_root, "context", "--run-id", "missing-workspace", "--json", cwd=checkout)
    assert inspected.returncode == 0, inspected.stderr
    payload = _payload(inspected)
    assert payload["run_id"] == "missing-workspace"
    assert payload["workspace_state"] == "missing"
    assert payload["ready"] is False


def test_successful_identical_activity_reuses_result_until_explicit_revision(linked_checkouts, tmp_path: Path) -> None:
    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    started = _start(state_root, checkout, "reuse result", "--run-id", "reuse-014")
    assert started.returncode == 0, started.stderr
    activity = _payload(started)["activity_id"]
    completed = _cli(
        state_root, "complete", "reuse-014", "--json",
        "--result-locator", "fixture://reuse-014/plan-result",
        "--result-sha256", "a" * 64, cwd=checkout,
    )
    assert completed.returncode == 0, completed.stderr
    completed_payload = _payload(completed)
    assert completed_payload["activity_state"] == "succeeded"
    assert completed_payload["run_state"] == "idle"
    reused = _cli(state_root, "start", "--skill", "fix", "--objective", "reuse result", "--run-id", "reuse-014", "--activity", "plan", "--json", cwd=checkout)
    assert reused.returncode == 0, reused.stderr
    reused_payload = _payload(reused)
    assert reused_payload["reused_result"] is True
    assert reused_payload["activity_id"] == activity
    assert reused_payload["result"] == {
        "locator": "fixture://reuse-014/plan-result", "sha256": "a" * 64,
    }

    execute = _cli(
        state_root, "start", "--skill", "fix", "--objective", "reuse result",
        "--run-id", "reuse-014", "--activity", "execute", "--json", cwd=checkout,
    )
    assert execute.returncode == 0, execute.stderr
    execute_payload = _payload(execute)
    assert execute_payload["activity_id"] != activity
    assert execute_payload["reused_result"] is False
    execute_completed = _cli(
        state_root, "complete", "reuse-014", "--json",
        "--result-locator", "fixture://reuse-014/execute-result",
        "--result-sha256", "b" * 64, cwd=checkout,
    )
    assert execute_completed.returncode == 0, execute_completed.stderr
    revised = _cli(state_root, "start", "--skill", "fix", "--objective", "reuse result v2", "--run-id", "reuse-014", "--activity", "plan", "--revise", "--json", cwd=checkout)
    assert revised.returncode == 0, revised.stderr
    assert _payload(revised)["activity_id"] not in {activity, execute_payload["activity_id"]}


def test_cli_context_activity_flows_through_facades_and_launch_store(
    linked_checkouts, tmp_path: Path,
) -> None:
    from gates import project_control_context as project_gate_context
    from run_context import (
        ContextRequest, objective_digest, repository_identity, resolve_context,
        resolve_repository,
    )
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest, reserve_launch,
        reserve_resources,
    )
    from scripts.coord.coord import project_control_context as project_coord_context

    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    objective = "canonical CLI activity"
    result = _start(
        state_root, checkout, objective, "--run-id", "canonical-activity",
    )
    assert result.returncode == 0, result.stderr
    emitted = _payload(result)
    repository = resolve_repository(checkout)
    repository_id = repository_identity(repository)
    store = ControlStore(state_root / "control.sqlite3")
    assert store.get_activity(emitted["activity_id"]).runtime_tuple_hash is None
    request = ContextRequest(
        cwd=checkout, operation="start", objective=objective,
        explicit_run_id="canonical-activity", activity="plan", inherited={},
    )
    context = resolve_context(request, store, repository_id)
    assert context.activity_id == emitted["activity_id"]
    read_store = ControlStore.open_read_only(store.db_path)
    coord = project_coord_context(context, read_store)
    gates = project_gate_context(context, read_store, {
        "candidate": "a" * 64, "runtime": "b" * 64, "config": "c" * 64,
        "policy": "d" * 64, "dependencies": "e" * 64,
    })
    assert coord["activity_id"] == gates["activity_id"] == emitted["activity_id"]
    assert coord["run_id"] == gates["run_id"] == emitted["run_id"]

    owned = reserve_resources(store, StartRequest(
        emitted["run_id"], emitted["workspace"], objective_digest(objective),
        ProcessIdentity.current(), repository_id=repository_id, planning_scope="",
    ))
    events_before_runtime = list(store.enumerate_events(
        run_id=emitted["run_id"], repository_id=repository_id,
    ))
    with pytest.raises(OwnershipRefused) as runtime_required:
        reserve_launch(store, emitted["activity_id"], owned.token)
    assert getattr(runtime_required.value, "code", None) == "RUNTIME_REQUIRED"
    assert list(store.enumerate_events(
        run_id=emitted["run_id"], repository_id=repository_id,
    )) == events_before_runtime

    executable = Path(sys.executable).resolve(strict=True)
    runtime_tuple = {
        "executable": str(executable),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "implementation": sys.implementation.name,
        "version": list(sys.version_info[:3]),
    }
    runtime_tuple_hash = hashlib.sha256(json.dumps(
        runtime_tuple, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    bound = store.bind_runtime(
        owned.token, emitted["activity_id"], runtime_tuple_hash,
    )
    assert bound.runtime_tuple_hash == runtime_tuple_hash
    intent = reserve_launch(store, emitted["activity_id"], owned.token)
    assert intent.activity_id == emitted["activity_id"]


def test_facades_reject_previous_activity_even_with_current_generation(
    linked_checkouts, tmp_path: Path,
) -> None:
    import dataclasses

    from gates import project_control_context as project_gate_context
    from run_context import ContextRequest, repository_identity, resolve_context, resolve_repository
    from run_state.state import ControlStore, ControlStoreRefused
    from scripts.coord.coord import project_control_context as project_coord_context

    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    objective = "stale facade activity"
    started = _start(
        state_root, checkout, objective, "--run-id", "stale-facade-activity",
    )
    assert started.returncode == 0, started.stderr
    previous_activity_id = _payload(started)["activity_id"]
    completed = _cli(
        state_root, "complete", "stale-facade-activity", "--json",
        "--result-locator", "fixture://stale-facade/plan",
        "--result-sha256", "7" * 64, cwd=checkout,
    )
    assert completed.returncode == 0, completed.stderr
    advanced = _cli(
        state_root, "start", "--skill", "fix", "--objective", objective,
        "--run-id", "stale-facade-activity", "--activity", "execute", "--json",
        cwd=checkout,
    )
    assert advanced.returncode == 0, advanced.stderr
    assert _payload(advanced)["activity_id"] != previous_activity_id

    repository_id = repository_identity(resolve_repository(checkout))
    store = ControlStore(state_root / "control.sqlite3")
    current = resolve_context(ContextRequest(
        cwd=checkout, operation="context", objective=objective,
        explicit_run_id="stale-facade-activity", inherited={},
    ), store, repository_id)
    assert current.activity_id == _payload(advanced)["activity_id"]
    read_store = ControlStore.open_read_only(store.db_path)
    hashes = {
        "candidate": "a" * 64, "runtime": "b" * 64, "config": "c" * 64,
        "policy": "d" * 64, "dependencies": "e" * 64,
    }
    assert project_coord_context(current, read_store)["activity_id"] == current.activity_id
    assert project_gate_context(current, read_store, hashes)["activity_id"] == current.activity_id

    stale = dataclasses.replace(current, activity_id=previous_activity_id)
    for project in (
        lambda: project_coord_context(stale, read_store),
        lambda: project_gate_context(stale, read_store, hashes),
    ):
        with pytest.raises(ControlStoreRefused) as refused:
            project()
        assert refused.value.code == "FENCE_REVOKED"


@pytest.mark.parametrize("terminal", ["failed", "aborted"])
def test_failed_or_aborted_cli_activity_never_reuses_success(
    linked_checkouts, tmp_path: Path, terminal: str,
) -> None:
    from run_context import objective_digest, repository_identity, resolve_repository
    from run_state.ownership import (
        ControlStore, ProcessIdentity, StartRequest, release_owner, reserve_resources,
    )

    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    run_id = f"terminal-cli-{terminal}"
    objective = f"terminal CLI {terminal}"
    started = _start(state_root, checkout, objective, "--run-id", run_id)
    assert started.returncode == 0, started.stderr
    payload = _payload(started)
    repository_id = repository_identity(resolve_repository(checkout))
    store = ControlStore(state_root / "control.sqlite3")
    owner = reserve_resources(store, StartRequest(
        run_id, payload["workspace"], objective_digest(objective),
        ProcessIdentity.current(), repository_id=repository_id, planning_scope="",
    ))
    store.transition_activity(
        owner.token, payload["activity_id"], expected="active", new=terminal,
    )
    with store.transaction() as tx:
        release_owner(tx, owner.token)

    replay = _start(state_root, checkout, objective, "--run-id", run_id)
    assert replay.returncode != 0
    refusal = _payload(replay)
    assert refusal["code"] == "REVISION_REQUIRED"
    assert refusal.get("reused_result") is not True


def test_anonymous_request_key_replays_the_minted_run(linked_checkouts, tmp_path: Path) -> None:
    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    first = _start(
        state_root, checkout, "anonymous keyed request",
        "--request-key", "anonymous-request-key",
    )
    assert first.returncode == 0, first.stderr
    first_payload = _payload(first)
    replay = _start(
        state_root, checkout, "anonymous keyed request",
        "--request-key", "anonymous-request-key",
    )
    assert replay.returncode == 0, replay.stderr
    assert _payload(replay) == first_payload
    porcelain = _git_text("worktree", "list", "--porcelain", cwd=checkout)
    assert porcelain.count(f"worktree {first_payload['workspace']}") == 1

    changed = _start(
        state_root, checkout, "anonymous keyed request changed",
        "--request-key", "anonymous-request-key",
    )
    assert changed.returncode != 0
    assert _payload(changed)["code"] == "IDEMPOTENCY_CONFLICT"


def test_complete_json_without_state_root_is_typed_and_uses_no_live_store(
    linked_checkouts, tmp_path: Path,
) -> None:
    checkout, _ = linked_checkouts
    home = tmp_path / "private-home"
    temp = home / "tmp"
    temp.mkdir(parents=True)
    env = _isolated_env()
    env.update(HOME=str(home), TMPDIR=str(temp))
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
    result = subprocess.run(
        [sys.executable, "-m", "run_state.cli", "complete", "missing-run", "--json"],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    refusal = _payload(result)
    assert refusal["ok"] is False
    assert refusal["code"] == "STATE_ROOT_REQUIRED"
    assert isinstance(refusal["recovery_action"], dict)
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_request_key_replays_same_start_and_rejects_changed_request(
    linked_checkouts, tmp_path: Path,
) -> None:
    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    first = _start(state_root, checkout, "idempotent request", "--run-id", "request-key",
                   "--request-key", "fixture-request-1")
    assert first.returncode == 0, first.stderr
    first_payload = _payload(first)
    replay = _start(state_root, checkout, "idempotent request", "--run-id", "request-key",
                    "--request-key", "fixture-request-1")
    assert replay.returncode == 0, replay.stderr
    assert _payload(replay) == first_payload

    changed = _start(state_root, checkout, "changed request", "--run-id", "request-key",
                     "--request-key", "fixture-request-1")
    assert changed.returncode == 2
    refusal = _payload(changed)
    assert refusal["code"] == "IDEMPOTENCY_CONFLICT"
    assert isinstance(refusal["recovery_action"], dict)
    worktrees = _git_text("worktree", "list", "--porcelain", cwd=checkout)
    assert worktrees.count("branch refs/heads/ffs/runs/request-key") == 1


def test_nonempty_selected_input_is_explicitly_unmet_not_silently_dropped(
    linked_checkouts, tmp_path: Path,
) -> None:
    checkout, _ = linked_checkouts
    state_root = tmp_path / "authority"
    result = _start(state_root, checkout, "selected input", "--run-id", "selected-input",
                    "--selected-input", "README.md")
    assert result.returncode == 5
    refusal = _payload(result)
    assert refusal["ok"] is False
    assert refusal["code"] == "SELECTED_INPUT_UNSUPPORTED"
    assert refusal["problem"] and refusal["cause"] and refusal["fix"] and refusal["docs"]
    assert isinstance(refusal["recovery_action"], dict)
    assert refusal["recovery_action"]["action"] == "remove_selected_inputs"
    assert "nonce" not in json.dumps(refusal).lower()


def _ownership_probe(state_root: Path, objective: str, run_id: str,
                     workspace: str, gate: Path | None = None,
                     *, hold: bool = False) -> subprocess.Popen[str]:
    program = textwrap.dedent("""
        import json, sys
        from pathlib import Path
        from run_state.ownership import (ControlStore, OwnershipRefused, ProcessIdentity,
                                         StartRequest, reserve_resources)
        root, objective, run_id, workspace, gate_raw, hold_raw = sys.argv[1:]
        root = Path(root)
        if gate_raw != "-":
            gate = Path(gate_raw)
            while not gate.exists():
                import time; time.sleep(.005)
        try:
            ownership = reserve_resources(ControlStore(root / "control.sqlite3"), StartRequest(
                run_id=run_id, workspace=str(root / workspace), objective_digest=objective,
                owner=ProcessIdentity.current()))
        except OwnershipRefused as error:
            print(json.dumps({"code": error.code})); raise SystemExit(3)
        print(json.dumps({"generation": ownership.generation, "run_id": ownership.run_id}), flush=True)
        if hold_raw == "hold":
            sys.stdin.readline()
    """)
    command = [sys.executable, "-c", program, str(state_root), objective, run_id,
               workspace, str(gate) if gate else "-", "hold" if hold else "exit"]
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=_isolated_env())


def _probe_line(process: subprocess.Popen[str]) -> dict:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10), (
            f"reservation result deadline exceeded; pid={process.pid} returncode={process.poll()}"
        )
    line = process.stdout.readline()
    if not line:
        diagnostic = "reservation emitted no output"
        if process.poll() is not None and process.stderr is not None:
            diagnostic = process.stderr.read()
        raise AssertionError(diagnostic)
    return json.loads(line)


def test_objective_reservation_race_admits_exactly_one_owner(tmp_path: Path) -> None:
    state_root = tmp_path / "authority"
    gate = tmp_path / "release-contenders"
    try:
        contenders = [
            _ownership_probe(state_root, "same-digest", f"race-{index}",
                             f"workspace-{index}", gate, hold=True)
            for index in range(2)
        ]
        gate.touch()
        payloads = [_probe_line(contender) for contender in contenders]
        for contender in contenders:
            if contender.poll() is None:
                assert contender.stdin is not None
                contender.stdin.write("release\n"); contender.stdin.flush()
        results = [subprocess.CompletedProcess(contender.args, contender.wait(timeout=10),
                                               json.dumps(payload), "")
                   for contender, payload in zip(contenders, payloads)]
    finally:
        for contender in locals().get("contenders", []):
            if contender.poll() is None:
                contender.terminate(); contender.wait(timeout=5)
    assert sum(result.returncode == 0 for result in results) == 1, [(r.returncode, r.stderr) for r in results]
    loser = next(result for result in results if result.returncode)
    assert json.loads(loser.stdout or loser.stderr)["code"] == "OWNER_LIVE"


@pytest.mark.parametrize(
    ("first_run", "first_workspace", "second_run", "second_workspace", "objective"),
    [
        ("run-014", "workspace-a", "run-014", "workspace-b", "run-digest"),
        ("run-a", "workspace-shared", "run-b", "workspace-shared", "workspace-digest"),
    ],
    ids=("run", "workspace"),
)
def test_live_run_or_workspace_owner_blocks_conflict(
    tmp_path: Path, first_run: str, first_workspace: str, second_run: str,
    second_workspace: str, objective: str,
) -> None:
    state_root = tmp_path / "authority"
    try:
        first = _ownership_probe(state_root, objective, first_run, first_workspace, hold=True)
        assert "generation" in _probe_line(first)
        second = _ownership_probe(state_root, objective + "-other", second_run, second_workspace)
        second_payload = _probe_line(second)
        second.wait(timeout=10)
    finally:
        if "first" in locals() and first.poll() is None:
            assert first.stdin is not None
            first.stdin.write("release\n"); first.stdin.flush(); first.wait(timeout=10)
    assert second.returncode != 0
    assert second_payload["code"] == "OWNER_LIVE"


def test_unknown_liveness_blocks_takeover_via_documented_probe_boundary(tmp_path: Path) -> None:
    from run_state.ownership import (ControlStore, OwnershipRefused, ProcessIdentity,
                                     StartRequest, reserve_resources)

    state_root = tmp_path / "authority"
    first = _ownership_probe(state_root, "unknown-digest", "unknown-014", "workspace")
    assert "generation" in _probe_line(first)
    assert first.wait(timeout=10) == 0
    store = ControlStore(state_root / "control.sqlite3", liveness_probe=lambda _identity: "UNKNOWN")
    with pytest.raises(OwnershipRefused) as rejected:
        reserve_resources(store, StartRequest("unknown-014", str(state_root / "workspace"),
                                             "unknown-digest", ProcessIdentity.current()))
    assert rejected.value.code == "OWNER_UNKNOWN"


def test_dead_owner_reclaim_increments_generation_and_fences_old_or_wrong_nonce(tmp_path: Path) -> None:
    """A real short-lived owner is reclaimed only after its OS identity dies."""
    import dataclasses
    from run_state.ownership import (ControlStore, OwnershipRefused, ProcessIdentity,
                                     StartRequest, assert_owner, reserve_resources)

    root = tmp_path / "authority"
    first = _ownership_probe(root, "fence-digest", "fence-014", "workspace")
    first_generation = _probe_line(first)["generation"]
    assert first.wait(timeout=10) == 0

    store = ControlStore(root / "control.sqlite3")
    second = reserve_resources(store, StartRequest(
        "fence-014", str(root / "workspace"), "fence-digest", ProcessIdentity.current()))
    assert second.generation > first_generation
    with pytest.raises(OwnershipRefused) as rejected:
        with store.transaction() as tx:
            assert_owner(tx, dataclasses.replace(second.token, nonce="wrong"))
    assert rejected.value.code == "FENCE_REVOKED"
