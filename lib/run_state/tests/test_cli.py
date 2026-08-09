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
    r = _run(
        ["start", "--skill", "fix", "--objective", "test bug"],
        env_extra={"RUN_STATE_DB": str(db)},
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["run_id"]


def test_status_command_returns_run_info(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
    started = _run(["start", "--skill", "fix", "--objective", "test"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]
    status = _run(["status", run_id], env_extra=env)
    assert status.returncode == 0
    payload = json.loads(status.stdout)
    assert payload["state"] == "active"
    assert payload["skill"] == "fix"


def test_complete_command_sets_state_complete(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
    started = _run(["start", "--skill", "fix", "--objective", "test"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]
    r = _run(["complete", run_id], env_extra=env)
    assert r.returncode == 0
    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "complete"


def test_audit_command_passes_verdict_pass(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
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

    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "complete"


def test_audit_command_fail_keeps_run_active(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
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
    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "active"


def test_update_state_complete_sets_state(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
    started = _run(["start", "--skill", "fix", "--objective", "x"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]
    _run(["update", run_id, "--state", "complete"], env_extra=env)
    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "complete"


def test_update_tokens_emits_a_single_machine_breach_line(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
    run_id = json.loads(_run(["start", "--skill", "fix", "--objective", "x", "--tokens", "10"], env_extra=env).stdout)["run_id"]
    assert _run(["update", run_id, "--tokens", "9"], env_extra=env).stdout == ""
    assert _run(["update", run_id, "--tokens", "1"], env_extra=env).stdout == f"BUDGET-BREACH: {run_id} 10 10\n"
    assert _run(["update", run_id, "--tokens", "1"], env_extra=env).stdout == ""


def test_feature_audit_pass_keeps_state_active(tmp_path: Path) -> None:
    """v3.0: feature audit pass keeps state=active (canary still pending)."""
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
    started = _run(["start", "--skill", "feature", "--objective", "spec-130"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/usr/bin/env bash\necho \'{"verdict":"pass","reasoning":"ok"}\'\n')
    stub.chmod(0o755)

    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    r = _run(["audit", run_id, "--kind", "feature",
              "--context", "SPEC_PATH=specs/130/plan.md",
              "--context", "SPEC_CONTENT=stub",
              "--context", "MODIFIED_FILES=none"],
             env_extra=env)
    assert r.returncode == 0, r.stderr
    status = _run(["status", run_id], env_extra=env)
    state = json.loads(status.stdout)["state"]
    assert state == "active", f"feature audit pass must leave state=active, got {state}"


def test_fix_audit_pass_completes(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
    started = _run(["start", "--skill", "fix", "--objective", "bug"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/usr/bin/env bash\necho \'{"verdict":"pass","reasoning":"ok"}\'\n')
    stub.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    r = _run(["audit", run_id, "--kind", "fix",
              "--context", "BUG_DESCRIPTION=x",
              "--context", "MODIFIED_FILES=none"], env_extra=env)
    assert r.returncode == 0
    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "complete"


# --- Group E: D1 phase audit -----------------------------------------------


def test_phase_audit_pass_keeps_active(tmp_path: Path) -> None:
    """D1: --kind phase pass keeps state=active — feature pipeline continues."""
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
    started = _run(["start", "--skill", "feature", "--objective", "spec-130"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/usr/bin/env bash\necho \'{"verdict":"pass","reasoning":"wedge done"}\'\n')
    stub.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    r = _run(["audit", run_id, "--kind", "phase",
              "--context", "PHASE_NAME=backend-wedge",
              "--context", "PRIOR_PHASES=none",
              "--context", "PHASE_SPEC=stub",
              "--context", "PHASE_DIFF=stub"], env_extra=env)
    assert r.returncode == 0, r.stderr
    status = _run(["status", run_id], env_extra=env)
    assert json.loads(status.stdout)["state"] == "active"


def test_phase_audit_fail_reverts_active(tmp_path: Path) -> None:
    """D1: --kind phase fail leaves state=active with audit_attempts incremented."""
    db = tmp_path / "runs.db"
    env = {"RUN_STATE_DB": str(db)}
    started = _run(["start", "--skill", "feature", "--objective", "spec"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text(
        '#!/usr/bin/env bash\n'
        'echo \'{"verdict":"fail","reasoning":"x","missing":["TODO-A"]}\'\n'
    )
    stub.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    r = _run(["audit", run_id, "--kind", "phase",
              "--context", "PHASE_NAME=w",
              "--context", "PRIOR_PHASES=none",
              "--context", "PHASE_SPEC=s",
              "--context", "PHASE_DIFF=d"], env_extra=env)
    assert r.returncode == 1
    status = json.loads(_run(["status", run_id], env_extra=env).stdout)
    assert status["state"] == "active"
    assert status["audit_attempts"] >= 1
    assert status["last_audit_verdict"] == "fail"


def test_audit_appends_jsonl_for_goal_grep(tmp_path: Path) -> None:
    """v3.0 codex-gate Pass 1 P2: audit must append to ~/.claude/state/audits.jsonl
    so native /goal condition checker can grep audit history."""
    db = tmp_path / "runs.db"
    fake_home = tmp_path / "fake-home"
    env = {"RUN_STATE_DB": str(db), "HOME": str(fake_home)}
    started = _run(["start", "--skill", "fix", "--objective", "x"], env_extra=env)
    run_id = json.loads(started.stdout)["run_id"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/usr/bin/env bash\necho \'{"verdict":"pass","reasoning":"audit ok","missing":[]}\'\n')
    stub.chmod(0o755)

    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    r = _run(["audit", run_id, "--kind", "fix",
              "--context", "BUG_DESCRIPTION=x",
              "--context", "MODIFIED_FILES=none"], env_extra=env)
    assert r.returncode == 0

    audits_log = fake_home / ".claude" / "state" / "audits.jsonl"
    assert audits_log.exists(), "audits.jsonl must be created"
    lines = [json.loads(l) for l in audits_log.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    record = lines[0]
    assert record["run_id"] == run_id
    assert record["verdict"] == "pass"
    assert record["kind"] == "fix"
    assert "ts" in record  # ISO-8601 timestamp
