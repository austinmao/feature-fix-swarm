import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[3] / "scripts" / "hooks" / "run-state-stop.py"
LIB_ROOT = Path(__file__).resolve().parents[2]


def _run_hook(env_extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{LIB_ROOT}:{env.get('PYTHONPATH', '')}"
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True, env=env)


def _cli(args, env_extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{LIB_ROOT}:{env.get('PYTHONPATH', '')}"
    env.update(env_extra)
    return subprocess.run([sys.executable, "-m", "run_state.cli", *args], capture_output=True, text=True, env=env)


def test_no_marker_allows_stop(tmp_path: Path) -> None:
    r = _run_hook({"RUN_STATE_MARKER": str(tmp_path / ".active-run"), "RUN_STATE_DB": str(tmp_path / "runs.db")})
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_active_run_blocks_stop_with_continuation(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _cli(["start", "--skill", "fix", "--objective", "block me"], env)
    assert started.returncode == 0, started.stderr
    r = _run_hook(env)
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["decision"] == "block"
    assert "block me" in payload["reason"]


def test_complete_run_allows_stop_and_clears_marker(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _cli(["start", "--skill", "fix", "--objective", "x"], env)
    run_id = json.loads(started.stdout)["run_id"]
    _cli(["complete", run_id], env)
    marker.write_text(run_id)
    r = _run_hook(env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    assert not marker.exists()


def test_budget_limited_allows_stop(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _cli(["start", "--skill", "fix", "--objective", "x", "--tokens", "100"], env)
    run_id = json.loads(started.stdout)["run_id"]
    _cli(["update", run_id, "--tokens", "200"], env)
    r = _run_hook(env)
    assert r.stdout.strip() == ""
