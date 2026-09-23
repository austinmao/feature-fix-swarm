"""Recovery contract for the pre-preparation versioned CLI crash boundary.

The initial process is killed after immutable selected input capture and the
durable context insert, but before ``begin_workspace_preparation`` performs
any work.  Resume must consume that retained capture and bind upstream against
the one durable workspace allocated by the original context.
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

from test_m4_upstream_cli_recovery_hardening import (
    _run_row,
    _versioned_start_argv,
)
from test_m4_upstream_context_acceptance import (
    _env,
    _manifest,
    _payload,
    _register,
    _registered_runtime,
    _repository,
    _sha,
    _start,
    _write_manifest,
)


def _crash_before_workspace_preparation(
    primary: Path,
    env: dict[str, str],
    argv: list[str],
) -> subprocess.CompletedProcess[str]:
    """Kill after CLI context persistence, at the real preparation boundary."""
    program = textwrap.dedent(
        """
        import json
        import os
        import signal
        import sys
        import run_state.cli as cli
        import run_state.workspace as workspace

        def stop_before_preparation(store, token, **kwargs):
            selected = kwargs["selected_input_manifest"]
            print(json.dumps({
                "boundary": "before-begin-workspace-preparation",
                "workspace": os.fspath(kwargs["workspace"]),
                "selected_schema": selected.get("schema"),
                "selection_manifest_hash": selected.get("selection_manifest_hash"),
                "input_digest": selected.get("input_digest"),
                "capture_locator": selected.get("capture", {}).get("locator"),
            }, sort_keys=True), flush=True)
            os.kill(os.getpid(), signal.SIGKILL)

        workspace.begin_workspace_preparation = stop_before_preparation
        raise SystemExit(cli.main(sys.argv[1:]))
        """
    )
    return subprocess.run(
        [sys.executable, "-c", program, *argv],
        cwd=primary,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _workspace_row(state_root: Path, repository_id: str, run_id: str) -> sqlite3.Row:
    connection = sqlite3.connect(state_root / "control.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM context_workspaces WHERE repository_id=? AND run_id=?",
            (repository_id, run_id),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


@pytest.mark.parametrize(
    "change_primary_after_crash",
    [False, True],
    ids=["unchanged-primary", "changed-primary"],
)
def test_resume_before_preparation_reuses_retained_selected_snapshot_and_binding(
    tmp_path: Path,
    change_primary_after_crash: bool,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = (
        "preparation-crash-changed-primary"
        if change_primary_after_crash
        else "preparation-crash-unchanged-primary"
    )
    original_selected = b"selected before context crash\n"
    selected_path = primary / "src" / "input.txt"
    selected_path.write_bytes(original_selected)
    selection_value = _manifest(
        primary,
        repository_id,
        selected=original_selected,
        upstream={
            "project": "project-key",
            "workstream": "backend",
            "session_key": "preparation-recovery-session",
        },
    )
    selection = _write_manifest(tmp_path, selection_value)
    runtime_path, runtime_sha256 = _registered_runtime()
    argv = _versioned_start_argv(
        state_root,
        selection,
        run_id,
        runtime_path=runtime_path,
        runtime_sha256=runtime_sha256,
    )

    crashed = _crash_before_workspace_preparation(primary, _env(tmp_path), argv)
    assert crashed.returncode == -signal.SIGKILL, (
        f"stdout={crashed.stdout!r}; stderr={crashed.stderr!r}"
    )
    barrier = json.loads(crashed.stdout)
    assert barrier["boundary"] == "before-begin-workspace-preparation"
    assert barrier["selected_schema"] == "ffs.input-snapshot/v1"
    assert barrier["selection_manifest_hash"] == _sha(
        json.dumps(
            selection_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    capture = Path(barrier["capture_locator"])
    assert capture.is_dir()
    assert (capture / "files" / "src" / "input.txt").read_bytes() == original_selected

    interrupted = _run_row(state_root, repository_id, run_id)
    assert interrupted["state"] == "preparing"
    assert interrupted["preparation_id"] is None
    assert interrupted["workspace"] == barrier["workspace"]
    assert interrupted["input_digest"] == barrier["input_digest"]
    assert json.loads(interrupted["upstream_json"]) == {
        "project": "project-key",
        "runtime_manifest_sha256": runtime_sha256,
        "session_key": "preparation-recovery-session",
        "workstream": "backend",
    }

    if change_primary_after_crash:
        selected_path.write_bytes(b"primary changed after durable capture\n")

    resumed = _start(
        state_root,
        primary,
        selection,
        run_id,
        env=_env(tmp_path),
        resume=True,
    )
    assert resumed.returncode == 0, (
        f"stdout={resumed.stdout!r}; stderr={resumed.stderr!r}"
    )
    payload = _payload(resumed)
    workspace = Path(payload["workspace"])
    assert payload["ready"] is True
    assert workspace == Path(interrupted["workspace"])
    assert payload["selected_input_count"] == 1
    assert payload["input_digest"] == interrupted["input_digest"]
    assert (workspace / "src" / "input.txt").read_bytes() == original_selected
    assert capture.is_dir()
    assert (capture / "files" / "src" / "input.txt").read_bytes() == original_selected
    if change_primary_after_crash:
        assert selected_path.read_bytes() == b"primary changed after durable capture\n"
    else:
        assert selected_path.read_bytes() == original_selected

    assert payload["upstream"]["project"] == "project-key"
    assert payload["upstream"]["workstream"] == "backend"
    assert payload["upstream"]["session_key"] == "preparation-recovery-session"
    assert payload["upstream"]["runtime_manifest_sha256"] == runtime_sha256
    assert payload["upstream"]["planning_root"] == str(
        workspace / ".planning" / "project-key" / "workstreams" / "backend"
    )
    assert len(payload["upstream"]["runtime_digest"]) == 64

    durable_workspace = _workspace_row(state_root, repository_id, run_id)
    selected_snapshot = json.loads(durable_workspace["selected_manifest_json"])
    assert selected_snapshot["schema"] == "ffs.input-snapshot/v1"
    assert selected_snapshot["selection_manifest_hash"] == barrier["selection_manifest_hash"]
    assert selected_snapshot["input_digest"] == barrier["input_digest"]
    assert selected_snapshot["capture"]["locator"] == str(capture)
    assert json.loads(_run_row(state_root, repository_id, run_id)["upstream_json"]) == payload["upstream"]
