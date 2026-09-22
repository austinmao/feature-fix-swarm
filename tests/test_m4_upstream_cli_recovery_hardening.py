"""Recovery and material-binding acceptance for versioned upstream CLI starts.

These cases extend the frozen 11-node upstream CLI contract.  All repository,
runtime-copy, state, and process effects are confined to ``tmp_path``.  The
controller-provided runtime is copied only to construct bounded missing/drifted
closure fixtures; the registered runtime itself is never modified.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from test_m4_upstream_context_acceptance import (
    LIB,
    _assert_refused,
    _cli,
    _empty_selection,
    _env,
    _manifest,
    _payload,
    _register,
    _registered_runtime,
    _repository,
    _runtime_flags,
    _sha,
    _start,
    _tree,
    _write_manifest,
)


def _runtime_copy_with_fault(tmp_path: Path, fault: str) -> tuple[Path, str]:
    """Copy the registered module closure, then remove or change one file."""
    descriptor_path, _ = _registered_runtime()
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    source_root = Path(descriptor["module_root"])
    copied_root = tmp_path / "runtime-copy" / "modules"
    copied_root.mkdir(mode=0o700, parents=True)
    for relative in sorted(descriptor["modules"]):
        destination = copied_root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes((source_root / relative).read_bytes())
        destination.chmod(0o600)
    target = copied_root / sorted(descriptor["modules"])[0]
    if fault == "missing":
        target.unlink()
    elif fault == "changed":
        target.write_bytes(target.read_bytes() + b"\n// fixture drift\n")
    else:  # pragma: no cover - the parametrized contract is closed.
        raise AssertionError(fault)
    descriptor["module_root"] = str(copied_root)
    copied_descriptor = tmp_path / f"runtime-{fault}.json"
    copied_descriptor.write_bytes(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    copied_descriptor.chmod(0o600)
    return copied_descriptor, _sha(copied_descriptor.read_bytes())


def _versioned_start_argv(
    state_root: Path,
    selection: Path,
    run_id: str,
    *,
    runtime_path: Path,
    runtime_sha256: str,
    resume: bool = False,
) -> list[str]:
    argv = [
        "start", "--skill", "fix", "--objective", f"upstream {run_id}",
        "--activity", "plan", "--run-id", run_id, "--scope", "m4-upstream",
        "--selection-manifest", str(selection), "--json",
        "--upstream-runtime-manifest", str(runtime_path),
        "--upstream-runtime-sha256", runtime_sha256,
        "--state-root", str(state_root),
    ]
    if resume:
        argv.append("--resume")
    return argv


def _patched_resolver_start(
    primary: Path,
    env: dict[str, str],
    argv: list[str],
    mode: str,
) -> subprocess.CompletedProcess[str]:
    """Run the public CLI with a fixed crash/refusal at the resolver boundary."""
    assert mode in {"crash", "refuse"}
    program = textwrap.dedent(
        """
        import json
        import os
        import signal
        import sys
        import run_state.upstream as upstream

        mode = sys.argv[1]
        if mode == "crash":
            def boundary(workspace, **kwargs):
                print(json.dumps({
                    "boundary": "before-upstream-binding",
                    "workspace": os.fspath(workspace),
                    "pid": os.getpid(),
                }), flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
        elif mode == "refuse":
            def boundary(workspace, **kwargs):
                raise upstream.UpstreamRefused("UPSTREAM_ESCAPE")
        else:
            raise SystemExit(97)
        upstream.resolve_upstream_binding = boundary
        from run_state.cli import main
        raise SystemExit(main(sys.argv[2:]))
        """
    )
    return subprocess.run(
        [sys.executable, "-c", program, mode, *argv],
        cwd=primary,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_row(state_root: Path, repository_id: str, run_id: str) -> sqlite3.Row:
    connection = sqlite3.connect(state_root / "control.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM context_runs WHERE repository_id=? AND run_id=?",
            (repository_id, run_id),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


@pytest.mark.parametrize("fault", ["missing", "changed"])
def test_closed_runtime_bytes_refuse_before_repository_state_or_workspace_effects(
    tmp_path: Path,
    fault: str,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = "aca1f581-2518-42fd-8d77-11615d5c6519"
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    runtime_path, runtime_sha256 = _runtime_copy_with_fault(tmp_path, fault)
    primary_before = _tree(primary)

    refused = _start(
        state_root,
        primary,
        selection,
        f"runtime-closure-{fault}",
        env=_env(tmp_path),
        runtime_flags=_runtime_flags(runtime_path, runtime_sha256),
    )

    _assert_refused(refused, "UPSTREAM_RUNTIME_DRIFT", 2)
    assert _tree(primary) == primary_before
    assert not state_root.exists()
    assert not (primary / ".git" / "ffs" / "repository.json").exists()
    assert not (primary.parent / ".ffs-workspaces").exists()


def test_versioned_empty_selection_resume_cannot_omit_manifest_or_runtime_pair(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = "versioned-material-required"
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    first = _start(state_root, primary, selection, run_id, env=_env(tmp_path))
    assert first.returncode == 0, f"stdout={first.stdout!r}; stderr={first.stderr!r}"
    workspace = Path(_payload(first)["workspace"])
    state_before = _tree(state_root)
    workspace_before = _tree(workspace)

    refused = _cli(
        state_root,
        primary,
        "start", "--skill", "fix", "--objective", f"upstream {run_id}",
        "--activity", "plan", "--run-id", run_id, "--scope", "m4-upstream",
        "--resume", "--json",
        env=_env(tmp_path),
    )

    _assert_refused(refused, "UPSTREAM_RUNTIME_REQUIRED", 2)
    assert _tree(state_root) == state_before
    assert _tree(workspace) == workspace_before


def test_versioned_default_session_resume_compares_the_derived_full_run_id(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = "derived-session-resume"
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    first = _start(state_root, primary, selection, run_id, env=_env(tmp_path))
    assert first.returncode == 0, f"stdout={first.stdout!r}; stderr={first.stderr!r}"
    first_payload = _payload(first)
    assert first_payload["upstream"]["session_key"] == run_id

    resumed = _start(
        state_root, primary, selection, run_id, env=_env(tmp_path), resume=True,
    )

    assert resumed.returncode == 0, f"stdout={resumed.stdout!r}; stderr={resumed.stderr!r}"
    resumed_payload = _payload(resumed)
    assert resumed_payload["upstream"] == first_payload["upstream"]
    assert resumed_payload["activity_id"] == first_payload["activity_id"]


@pytest.mark.parametrize("change", ["input-digest", "manifest-only"])
def test_direct_run_resume_rejects_changed_selection_material_before_effects(
    tmp_path: Path,
    change: str,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = f"direct-selection-{change}"
    selected = b"selected-v1\n"
    (primary / "src" / "input.txt").write_bytes(selected)
    initial_value = _manifest(
        primary,
        repository_id,
        selected=selected,
        upstream={"project": None, "workstream": None, "session_key": "direct-session"},
    )
    initial = _write_manifest(tmp_path, initial_value, "initial-selection.json")
    first = _start(state_root, primary, initial, run_id, env=_env(tmp_path))
    assert first.returncode == 0, f"stdout={first.stdout!r}; stderr={first.stderr!r}"
    workspace = Path(_payload(first)["workspace"])

    changed_value = json.loads(json.dumps(initial_value))
    if change == "input-digest":
        changed_bytes = b"selected-v2\n"
        (primary / "src" / "input.txt").write_bytes(changed_bytes)
        changed_value["entries"][0]["sha256"] = _sha(changed_bytes)
    else:
        changed_value["required_context"] = [{
            "path": ".planning/tracked-context.txt",
            "reason": "changed manifest-only requirement",
        }]
    changed = _write_manifest(tmp_path, changed_value, "changed-selection.json")
    state_before = _tree(state_root)
    workspace_before = _tree(workspace)

    refused = _start(
        state_root, primary, changed, run_id, env=_env(tmp_path), resume=True,
    )

    _assert_refused(refused, "UPSTREAM_CHANGED", 3)
    assert _tree(state_root) == state_before
    assert _tree(workspace) == workspace_before


def test_crash_before_binding_recovery_resolves_real_workspace_before_ready(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = "upstream-binding-crash"
    selection = _write_manifest(
        tmp_path,
        _manifest(
            primary,
            repository_id,
            upstream={
                "project": "project-key",
                "workstream": "backend",
                "session_key": "crash-session",
            },
        ),
    )
    runtime_path, runtime_sha256 = _registered_runtime()
    argv = _versioned_start_argv(
        state_root,
        selection,
        run_id,
        runtime_path=runtime_path,
        runtime_sha256=runtime_sha256,
    )

    crashed = _patched_resolver_start(primary, _env(tmp_path), argv, "crash")
    assert crashed.returncode == -signal.SIGKILL
    barrier = json.loads(crashed.stdout)
    assert barrier["boundary"] == "before-upstream-binding"
    assert barrier["pid"] > 0
    workspace = Path(barrier["workspace"])
    assert workspace.is_dir()
    interrupted = _run_row(state_root, repository_id, run_id)
    assert interrupted["state"] == "preparing"
    partial = json.loads(interrupted["upstream_json"])
    assert partial == {
        "project": "project-key",
        "runtime_manifest_sha256": runtime_sha256,
        "session_key": "crash-session",
        "workstream": "backend",
    }

    resumed = _start(
        state_root, primary, selection, run_id, env=_env(tmp_path), resume=True,
    )
    assert resumed.returncode == 0, f"stdout={resumed.stdout!r}; stderr={resumed.stderr!r}"
    payload = _payload(resumed)
    assert payload["ready"] is True
    assert Path(payload["workspace"]) == workspace
    assert payload["upstream"] == {
        "project": "project-key",
        "workstream": "backend",
        "session_key": "crash-session",
        "effective_session_key": "gsd-session-key-crash-session",
        "planning_root": str(workspace / ".planning" / "project-key" / "workstreams" / "backend"),
        "resolver_version": "1.14.0",
        "runtime_digest": payload["upstream"]["runtime_digest"],
        "runtime_manifest_sha256": runtime_sha256,
    }
    assert len(payload["upstream"]["runtime_digest"]) == 64
    durable = json.loads(_run_row(state_root, repository_id, run_id)["upstream_json"])
    assert durable == payload["upstream"]


def test_resolver_refusal_records_failure_releases_owner_and_retry_rebinds(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = "upstream-resolver-refusal"
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    runtime_path, runtime_sha256 = _registered_runtime()
    argv = _versioned_start_argv(
        state_root,
        selection,
        run_id,
        runtime_path=runtime_path,
        runtime_sha256=runtime_sha256,
    )

    refused = _patched_resolver_start(primary, _env(tmp_path), argv, "refuse")
    _assert_refused(refused, "UPSTREAM_ESCAPE", 2)
    failed = _run_row(state_root, repository_id, run_id)
    assert failed["state"] == "blocked"
    with sqlite3.connect(state_root / "control.sqlite3") as connection:
        held = connection.execute(
            "SELECT COUNT(*) FROM control_reservations WHERE held=1"
        ).fetchone()[0]
    assert held == 0

    retried = _start(
        state_root, primary, selection, run_id, env=_env(tmp_path), resume=True,
    )
    assert retried.returncode == 0, f"stdout={retried.stdout!r}; stderr={retried.stderr!r}"
    payload = _payload(retried)
    assert payload["ready"] is True
    assert set(payload["upstream"]) == {
        "project", "workstream", "session_key", "effective_session_key",
        "planning_root", "resolver_version", "runtime_digest",
        "runtime_manifest_sha256",
    }
    assert payload["upstream"]["planning_root"] == str(Path(payload["workspace"]) / ".planning")
    assert payload["upstream"]["runtime_manifest_sha256"] == runtime_sha256


def test_selection_manifest_alone_enters_versioned_route_and_requires_runtime(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    selection = _write_manifest(
        tmp_path,
        _empty_selection(primary, "4480ddea-fe81-4bfd-a1c5-98c8f904c54f"),
    )
    legacy_db = tmp_path / "legacy.sqlite3"
    env = _env(tmp_path)
    env["RUN_STATE_DB"] = str(legacy_db)
    primary_before = _tree(primary)

    refused = subprocess.run(
        [
            sys.executable, "-m", "run_state.cli", "start",
            "--skill", "fix", "--objective", "selection-only-versioned",
            "--selection-manifest", str(selection),
        ],
        cwd=primary,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    _assert_refused(refused, "UPSTREAM_RUNTIME_REQUIRED", 2)
    assert not legacy_db.exists()
    assert _tree(primary) == primary_before
    assert not (primary / ".git" / "ffs" / "repository.json").exists()
