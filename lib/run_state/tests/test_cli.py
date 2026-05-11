import json
import os
import subprocess
import sys
from pathlib import Path

LIB_ROOT = Path(__file__).resolve().parents[2]


def _run(args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{LIB_ROOT}:{env.get('PYTHONPATH', '')}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "run_state.cli", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_start_command_emits_run_id(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    r = _run(
        ["start", "--skill", "fix", "--objective", "test bug"],
        env_extra={"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)},
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["run_id"]
    assert marker.read_text().strip() == payload["run_id"]


def test_status_command_returns_run_info(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _run(["start", "--skill", "fix", "--objective", "test"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]
    status = _run(["status", run_id], env_extra=env)
    assert status.returncode == 0
    payload = json.loads(status.stdout)
    assert payload["state"] == "active"
    assert payload["skill"] == "fix"


def test_complete_command_clears_marker(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _run(["start", "--skill", "fix", "--objective", "test"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]
    r = _run(["complete", run_id], env_extra=env)
    assert r.returncode == 0
    assert not marker.exists()
    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "complete"


def test_pause_resume_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _run(["start", "--skill", "feature", "--objective", "test"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]
    _run(["pause", run_id], env_extra=env)
    assert not marker.exists()
    _run(["resume", run_id], env_extra=env)
    assert marker.exists()
    status = json.loads(_run(["status", run_id], env_extra=env).stdout)
    assert status["state"] == "active"


def test_audit_command_passes_verdict_pass(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _run(["start", "--skill", "fix", "--objective", "x"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/usr/bin/env bash\necho \'{"verdict":"pass","reasoning":"stubbed"}\'\n')
    stub.chmod(0o755)

    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    r = _run(["audit", run_id, "--kind", "fix",
              "--context", "BUG_DESCRIPTION=stub",
              "--context", "MODIFIED_FILES=none"],
             env_extra=env)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["verdict"] == "pass"
    assert not marker.exists()

    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "complete"


def test_audit_command_fail_keeps_run_active(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _run(["start", "--skill", "fix", "--objective", "x"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/usr/bin/env bash\necho \'{"verdict":"fail","reasoning":"repro found","missing":["X"]}\'\n')
    stub.chmod(0o755)

    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    r = _run(["audit", run_id, "--kind", "fix",
              "--context", "BUG_DESCRIPTION=stub",
              "--context", "MODIFIED_FILES=none"],
             env_extra=env)
    assert r.returncode == 1
    assert marker.exists()
    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "active"
