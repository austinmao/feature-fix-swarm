"""CLI acceptance for durable workspace-scoped upstream resolution.

The native test operator maps its registered runtime descriptor into explicit
CLI flags. Production code must not read the FFS_TEST_* locator variables.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
RUNTIME_ENV = "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR"
RUNTIME_SHA_ENV = "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256"
STRIPPED_ENV = (
    RUNTIME_ENV, RUNTIME_SHA_ENV, "GSD_PROJECT", "GSD_WORKSTREAM",
    "GSD_SESSION_KEY", "NODE_OPTIONS", "NODE_PATH", "FFS_RUN_ID", "GSD_RUN_ID",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    )
    return completed.stdout.strip()


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in STRIPPED_ENV:
        env.pop(name, None)
    home = tmp_path / "home"
    temporary = home / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(home), TMPDIR=str(temporary), PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=f"{LIB}:{env.get('PYTHONPATH', '')}",
        GIT_CONFIG_GLOBAL=str(home / "gitconfig"), GIT_CONFIG_SYSTEM=os.devnull,
    )
    return env


def _registered_runtime() -> tuple[Path, str]:
    path_value = os.environ.get(RUNTIME_ENV)
    expected = os.environ.get(RUNTIME_SHA_ENV)
    assert path_value and expected, "UNMET: controller-registered upstream runtime required"
    path = Path(path_value)
    assert path.is_absolute()
    assert len(expected) == 64
    assert _sha(path.read_bytes()) == expected
    return path, expected


def _runtime_flags(path: Path | None = None, digest: str | None = None) -> list[str]:
    registered_path, registered_digest = _registered_runtime()
    path = registered_path if path is None else path
    digest = registered_digest if digest is None else digest
    return [
        "--upstream-runtime-manifest", str(path),
        "--upstream-runtime-sha256", digest,
    ]


def _repository(tmp_path: Path) -> Path:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", cwd=primary)
    _git("config", "user.email", "upstream-context@example.test", cwd=primary)
    _git("config", "user.name", "Upstream Context", cwd=primary)
    (primary / "src").mkdir()
    (primary / "src" / "input.txt").write_bytes(b"base input\n")
    (primary / ".planning").mkdir()
    (primary / ".planning" / "tracked-context.txt").write_bytes(b"tracked\n")
    _git("add", ".planning", "src", cwd=primary)
    _git("commit", "-qm", "fixture base", cwd=primary)
    return primary


def _register(primary: Path, state_root: Path) -> str:
    from run_context import register_repository, resolve_repository
    from run_state.ownership import ControlStore

    state_root.mkdir(mode=0o700)
    store = ControlStore(state_root / "control.sqlite3")
    return register_repository(store, resolve_repository(primary), state_root)


def _manifest(
    primary: Path, repository_id: str, *, upstream: dict, selected: bytes | None = None,
) -> dict:
    entries = [] if selected is None else [{
        "operation": "copy", "path": "src/input.txt",
        "sha256": _sha(selected), "git_mode": "100644",
    }]
    return {
        "schema": "ffs.input-selection/v1",
        "base_oid": _git("rev-parse", "HEAD", cwd=primary),
        "repository_id": repository_id,
        "entries": entries,
        "required_context": [],
        "upstream": upstream,
    }


def _write_manifest(tmp_path: Path, value: dict, name: str = "selection.json") -> Path:
    path = tmp_path / name
    path.write_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    path.chmod(0o600)
    return path


def _cli(
    state_root: Path, primary: Path, *args: str, env: dict[str, str], timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "run_state.cli", *args, "--state-root", str(state_root)],
        cwd=primary, env=env, capture_output=True, text=True, timeout=timeout,
    )


def _start(
    state_root: Path, primary: Path, selection: Path, run_id: str, *,
    env: dict[str, str], activity: str = "plan", resume: bool = False,
    runtime_flags: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [
        "start", "--skill", "fix", "--objective", f"upstream {run_id}",
        "--activity", activity, "--run-id", run_id, "--scope", "m4-upstream",
        "--selection-manifest", str(selection), "--json",
        *(runtime_flags if runtime_flags is not None else _runtime_flags()),
    ]
    if resume:
        args.append("--resume")
    return _cli(state_root, primary, *args, env=env)


def _payload(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout.strip(), f"stderr={result.stderr!r}"
    return json.loads(result.stdout)


def _assert_refused(result: subprocess.CompletedProcess[str], code: str, exit_status: int) -> dict:
    assert result.returncode == exit_status, (
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    value = _payload(result)
    assert value["ok"] is False
    assert value["code"] == code
    return value


def _tree(path: Path) -> dict[str, tuple]:
    if not path.exists() and not path.is_symlink():
        return {}
    result: dict[str, tuple] = {}
    for item in sorted([path, *path.rglob("*")], key=lambda value: str(value)):
        info = item.lstat()
        relative = "." if item == path else str(item.relative_to(path))
        if stat.S_ISREG(info.st_mode):
            result[relative] = ("file", stat.S_IMODE(info.st_mode), _sha(item.read_bytes()))
        elif stat.S_ISLNK(info.st_mode):
            result[relative] = ("symlink", os.readlink(item))
        else:
            result[relative] = ("other", stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode))
    return result


def _empty_selection(primary: Path, repository_id: str, *, session_key=None) -> dict:
    return _manifest(
        primary, repository_id,
        upstream={"project": None, "workstream": None, "session_key": session_key},
    )


@pytest.mark.parametrize("pair", ["absent", "manifest-only", "sha-only"])
def test_versioned_clean_start_requires_complete_runtime_pair_before_effects(
    tmp_path: Path, pair: str,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    selection = _write_manifest(
        tmp_path,
        _empty_selection(primary, "4a66dd8e-cae9-44fc-bbab-a6fe065c4832"),
    )
    runtime_path, runtime_sha = _registered_runtime()
    flags = {
        "absent": [],
        "manifest-only": ["--upstream-runtime-manifest", str(runtime_path)],
        "sha-only": ["--upstream-runtime-sha256", runtime_sha],
    }[pair]
    before = _tree(primary)
    env = _env(tmp_path)
    env["FFS_UPSTREAM_RUNTIME_MANIFEST"] = str(runtime_path)
    env["NODE_OPTIONS"] = "--require=/ambient/not-authority.cjs"
    result = _start(
        state_root, primary, selection, f"runtime-required-{pair}",
        env=env, runtime_flags=flags,
    )
    _assert_refused(result, "UPSTREAM_RUNTIME_REQUIRED", 2)
    assert _tree(primary) == before
    assert not state_root.exists()
    assert not (primary / ".git" / "ffs" / "repository.json").exists()


@pytest.mark.parametrize("problem", ["hash", "shape"])
def test_invalid_runtime_refuses_before_repository_or_workspace_effects(
    tmp_path: Path, problem: str,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    selection = _write_manifest(
        tmp_path,
        _empty_selection(primary, "388ca632-d3bd-47e6-a012-3595145b56a0"),
    )
    runtime_path, runtime_sha = _registered_runtime()
    if problem == "hash":
        flags = _runtime_flags(runtime_path, "f" * 64 if runtime_sha != "f" * 64 else "e" * 64)
    else:
        malformed = tmp_path / "malformed-runtime.json"
        malformed.write_bytes(b"{}\n")
        malformed.chmod(0o600)
        flags = _runtime_flags(malformed, _sha(malformed.read_bytes()))
    before = _tree(primary)
    result = _start(
        state_root, primary, selection, f"runtime-drift-{problem}",
        env=_env(tmp_path), runtime_flags=flags,
    )
    _assert_refused(result, "UPSTREAM_RUNTIME_DRIFT", 2)
    assert _tree(primary) == before
    assert not state_root.exists()
    assert not (primary / ".git" / "ffs" / "repository.json").exists()


def test_selected_start_resolves_real_workspace_scope_and_preserves_primary_pointer(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    selected = b"selected input\n"
    (primary / "src" / "input.txt").write_bytes(selected)
    pointer = primary / ".planning" / "active-workstream"
    pointer.write_bytes(b"primary-pointer-unchanged\n")
    selection = _write_manifest(
        tmp_path,
        _manifest(
            primary, repository_id, selected=selected,
            upstream={
                "project": "project-key", "workstream": "backend",
                "session_key": "selected-session",
            },
        ),
    )
    selected_before = (primary / "src" / "input.txt").read_bytes()
    env = _env(tmp_path)
    global_pointer_state_before = _tree(Path(env["HOME"]))
    result = _start(
        state_root, primary, selection, "selected-upstream", env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    payload = _payload(result)
    workspace = Path(payload["workspace"])
    runtime_path, runtime_sha = _registered_runtime()
    assert payload["ready"] is True
    assert payload["selected_input_count"] == 1
    assert (workspace / "src" / "input.txt").read_bytes() == selected
    assert payload["upstream"] == {
        "project": "project-key", "workstream": "backend",
        "session_key": "selected-session",
        "effective_session_key": "gsd-session-key-selected-session",
        "planning_root": str(workspace / ".planning" / "project-key" / "workstreams" / "backend"),
        "resolver_version": "1.14.0",
        "runtime_digest": payload["upstream"]["runtime_digest"],
        "runtime_manifest_sha256": runtime_sha,
    }
    assert len(payload["upstream"]["runtime_digest"]) == 64
    assert runtime_path.is_absolute()
    assert (primary / "src" / "input.txt").read_bytes() == selected_before
    assert _tree(Path(env["HOME"])) == global_pointer_state_before
    assert pointer.read_bytes() == b"primary-pointer-unchanged\n"


def test_clean_versioned_start_derives_stable_full_run_session_and_context_needs_no_runtime(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = "r" * 64
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    started = _start(state_root, primary, selection, run_id, env=_env(tmp_path))
    assert started.returncode == 0, f"stdout={started.stdout!r}; stderr={started.stderr!r}"
    payload = _payload(started)
    workspace = Path(payload["workspace"])
    upstream = payload["upstream"]
    assert upstream["project"] is None and upstream["workstream"] is None
    assert upstream["session_key"] == run_id
    assert upstream["effective_session_key"] == f"gsd-session-key-{run_id}"
    assert upstream["planning_root"] == str(workspace / ".planning")
    runtime_sha = _registered_runtime()[1]
    assert upstream["runtime_manifest_sha256"] == runtime_sha

    inspected = _cli(
        state_root, primary, "context", "--run-id", run_id, "--json",
        env=_env(tmp_path),
    )
    assert inspected.returncode == 0, inspected.stderr
    inspected_payload = _payload(inspected)
    assert inspected_payload["code"] == "RUN_CONTEXT"
    assert inspected_payload["upstream"] == upstream


def test_same_run_plan_execute_and_resume_reuse_exact_persisted_upstream_material(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = "upstream-lifecycle"
    selection = _write_manifest(
        tmp_path,
        _manifest(
            primary, repository_id,
            upstream={
                "project": "project-key", "workstream": "backend",
                "session_key": "lifecycle-session",
            },
        ),
    )
    env = _env(tmp_path)
    plan = _start(state_root, primary, selection, run_id, env=env)
    assert plan.returncode == 0, f"stdout={plan.stdout!r}; stderr={plan.stderr!r}"
    plan_payload = _payload(plan)
    completed = _cli(
        state_root, primary, "complete", run_id, "--json",
        "--result-locator", "fixture://upstream/plan",
        "--result-sha256", "a" * 64, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    execute = _start(
        state_root, primary, selection, run_id, activity="execute", env=env,
    )
    assert execute.returncode == 0, f"stdout={execute.stdout!r}; stderr={execute.stderr!r}"
    execute_payload = _payload(execute)
    assert execute_payload["activity_id"] != plan_payload["activity_id"]
    assert execute_payload["upstream"] == plan_payload["upstream"]
    resumed = _start(
        state_root, primary, selection, run_id, activity="execute", resume=True, env=env,
    )
    assert resumed.returncode == 0, f"stdout={resumed.stdout!r}; stderr={resumed.stderr!r}"
    resumed_payload = _payload(resumed)
    assert resumed_payload["activity_id"] == execute_payload["activity_id"]
    assert resumed_payload["upstream"] == plan_payload["upstream"]


def test_resume_with_changed_runtime_bytes_refuses_without_state_or_workspace_effects(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = "upstream-runtime-change"
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    env = _env(tmp_path)
    first = _start(state_root, primary, selection, run_id, env=env)
    assert first.returncode == 0, f"stdout={first.stdout!r}; stderr={first.stderr!r}"
    payload = _payload(first)
    runtime_path, _ = _registered_runtime()
    changed = tmp_path / "same-runtime-different-bytes.json"
    changed.write_bytes(runtime_path.read_bytes() + b" \n")
    changed.chmod(0o600)
    before_state = _tree(state_root)
    before_workspace = _tree(Path(payload["workspace"]))
    refused = _start(
        state_root, primary, selection, run_id, resume=True, env=env,
        runtime_flags=_runtime_flags(changed, _sha(changed.read_bytes())),
    )
    _assert_refused(refused, "UPSTREAM_RUNTIME_CHANGED", 3)
    assert _tree(state_root) == before_state
    assert _tree(Path(payload["workspace"])) == before_workspace


def test_invalid_scope_refuses_before_registration(tmp_path: Path) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    selection = _write_manifest(
        tmp_path,
        _manifest(
            primary, "8165fe6e-0e64-4be1-8347-28b863bfca27",
            upstream={
                "project": "../foreign", "workstream": "backend",
                "session_key": "session",
            },
        ),
    )
    before = _tree(primary)
    refused = _start(
        state_root, primary, selection, "invalid-upstream-scope", env=_env(tmp_path),
    )
    _assert_refused(refused, "INVALID_UPSTREAM_SCOPE", 2)
    assert _tree(primary) == before
    assert not state_root.exists()


def test_workspace_planning_symlink_escape_never_reaches_ready_or_touches_foreign_root(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel.bin"
    sentinel.write_bytes(b"foreign unchanged\n")
    shutil.rmtree(primary / ".planning")
    (primary / ".planning").symlink_to(foreign, target_is_directory=True)
    _git("add", "-A", cwd=primary)
    _git("commit", "-qm", "fixture planning escape", cwd=primary)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    refused = _start(
        state_root, primary, selection, "planning-symlink-escape", env=_env(tmp_path),
    )
    _assert_refused(refused, "UPSTREAM_ESCAPE", 2)
    assert sentinel.read_bytes() == b"foreign unchanged\n"
    assert (primary / ".planning").is_symlink()
    with sqlite3.connect(state_root / "control.sqlite3") as connection:
        row = connection.execute(
            "SELECT state FROM context_runs WHERE repository_id=? AND run_id=?",
            (repository_id, "planning-symlink-escape"),
        ).fetchone()
    assert row is None or row[0] != "ready"
