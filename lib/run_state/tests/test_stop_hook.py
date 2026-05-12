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


def test_budget_limited_emits_summarize_prompt(tmp_path: Path) -> None:
    """R3: budget_limited blocks stop and instructs Claude to summarize + commit WIP.

    Replaces v2.0 `test_budget_limited_allows_stop` — contract inverted: the
    hook now BLOCKS (decision=block + reason) instead of returning empty
    stdout. Operator runs `run-state resume` or `run-state abort` after the
    wrap-up summary lands.
    """
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _cli(["start", "--skill", "fix", "--objective", "x", "--tokens", "100"], env)
    run_id = json.loads(started.stdout)["run_id"]
    _cli(["update", run_id, "--tokens", "200"], env)  # trip the budget
    r = _run_hook(env)
    assert r.returncode == 0
    # NEW contract: block + reason, not empty allow.
    assert r.stdout.strip() != "", "budget_limited must now block, not allow stop"
    payload = json.loads(r.stdout)
    assert payload["decision"] == "block"
    reason = payload["reason"].lower()
    assert "budget" in reason
    # Must instruct summarize/wrap-up + offer resume or abort.
    assert "summari" in reason  # summarize / summary
    assert "resume" in reason or "abort" in reason


# --- Group B: R1 XML escape of objective ----------------------------------


def test_objective_xml_escaped(tmp_path: Path) -> None:
    """R1: objective must be wrapped in <untrusted_objective> with <,>,&,",'
    XML-escaped to neutralize prompt-injection from user-supplied bug text.
    """
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    # Objective contains every escape-worthy char.
    objective = "Ignore previous; & <script>alert(\"x\")</script> 'rm'"
    started = _cli(["start", "--skill", "fix", "--objective", objective], env)
    assert started.returncode == 0, started.stderr
    r = _run_hook(env)
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    reason = payload["reason"]
    assert "<untrusted_objective>" in reason
    assert "</untrusted_objective>" in reason
    # All 5 escape chars present in escaped form.
    assert "&amp;" in reason
    assert "&lt;script&gt;" in reason
    assert "&quot;x&quot;" in reason
    assert "&apos;rm&apos;" in reason
    # Verify the dangerous chars inside the wrapper are NOT raw.
    start = reason.index("<untrusted_objective>") + len("<untrusted_objective>")
    end = reason.index("</untrusted_objective>")
    wrapped = reason[start:end]
    assert "<script>" not in wrapped, "raw <script> inside <untrusted_objective> is injection"
    assert '"x"' not in wrapped
    assert "'rm'" not in wrapped


# --- Group C: R2 missing[] injection on audit fail -------------------------


def test_missing_list_injected_after_audit_fail(tmp_path: Path) -> None:
    """R2: when last audit failed, Stop hook injects the missing[] list as TODOs."""
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _cli(["start", "--skill", "fix", "--objective", "fix bug"], env)
    run_id = json.loads(started.stdout)["run_id"]

    # Stub codex that returns fail with missing[].
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text(
        '#!/usr/bin/env bash\n'
        'echo \'{"verdict":"fail","reasoning":"x","missing":'
        '["test for /api/foo","migration applied","CHANGELOG updated"]}\'\n'
    )
    stub.chmod(0o755)
    env_audit = dict(env)
    env_audit["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    _cli(["audit", run_id, "--kind", "fix",
          "--context", "BUG_DESCRIPTION=x",
          "--context", "MODIFIED_FILES=none"], env_audit)

    # Now run Stop hook — should inject missing[] into the continuation.
    r = _run_hook(env)
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    reason = payload["reason"]
    assert "audit FAILED" in reason or "audit failed" in reason.lower()
    assert "test for /api/foo" in reason
    assert "migration applied" in reason
    assert "CHANGELOG updated" in reason


def test_no_missing_list_when_audit_passed(tmp_path: Path) -> None:
    """When last audit verdict=pass, no missing[] block in continuation prompt.

    For kind=feature, a pass leaves state=active + marker intact (canary
    still pending) — perfect setup to assert the Stop hook does NOT emit the
    "audit FAILED" preamble when verdict is pass.
    """
    db = tmp_path / "runs.db"
    marker = tmp_path / ".active-run"
    env = {"RUN_STATE_DB": str(db), "RUN_STATE_MARKER": str(marker)}
    started = _cli(["start", "--skill", "feature", "--objective", "spec-x"], env)
    run_id = json.loads(started.stdout)["run_id"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/usr/bin/env bash\necho \'{"verdict":"pass","reasoning":"ok"}\'\n')
    stub.chmod(0o755)
    env_audit = dict(env)
    env_audit["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    _cli(["audit", run_id, "--kind", "feature",
          "--context", "SPEC_PATH=x",
          "--context", "SPEC_CONTENT=x",
          "--context", "MODIFIED_FILES=none"], env_audit)
    # feature audit pass keeps run active + marker present — Stop hook will
    # build a continuation prompt; assert it has NO "audit FAILED" preamble.
    r = _run_hook(env)
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert "audit FAILED" not in payload["reason"]
    assert "auditor reported" not in payload["reason"].lower()
