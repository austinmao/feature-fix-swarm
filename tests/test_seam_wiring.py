"""Seam-wiring integration tests (spec-007 phase 4, plan 04-01).

INT-002 — promote-emit EMITS (never runs) the staging→prod promote command:
subprocess-driven against scripts/gsd/promote-emit.sh with an isolated
$GATES_STORE fixture store. The emitted string must carry the VERBATIM
gates.py promote flag set (lib/gates.py:2885-2897) — drift = unrunnable emit.

Wall-residual pins carried into this suite (WALL-RESIDUALS.md):
- 1ea31e0b: evidence selection reuses the EXISTING store schema — top-level
  gate records carry exit_code/executed_by/artifact (_evidence_resolves
  semantics, gates.py:1003-1014); `_promotions[run_id]` records carry the only
  run+surface binding the store has (surface/artifact/evidence_ids/recorded_at).
- 156cf77a: emission binds to run AND surface — a passing artifact claimed by
  another run's promotion records must never appear in this run's emit.
- 7cec17e8: multiple passing artifacts for one run+surface → LATEST by ts wins
  (promotion recorded_at, else store insertion order) and the skipped
  candidates are NAMED on stderr — deterministic, auditable.
- 31a608dc: every interpolated value is shape-gated BEFORE emission and
  single-quoted in the emitted command; non-conforming store values → typed
  value-free refusal, nothing emitted.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EMIT = ROOT / "scripts" / "gsd" / "promote-emit.sh"
GATES = ROOT / "lib" / "gates.py"

DIG_A = "app@sha256:" + "a" * 64
DIG_B = "app@sha256:" + "b" * 64


def _gate(artifact=None, exit_code=0, executed_by="run_gate"):
    g = {"exit_code": exit_code, "cmd": "python3 -m pytest -q",
         "tests_before": "", "tests_after": "ok", "failure_sig": "",
         "executed_by": executed_by}
    if artifact is not None:
        g["artifact"] = artifact
    return {"gate": g}


def _grants(actions, run="run-42"):
    now = time.time()
    return {run: {"grants": {a: {"granted_at": now,
                                 "expires_at": now + 3600,
                                 "granted_by": "operator"} for a in actions}}}


def _promo(surface, artifact, evidence_ids, recorded_at):
    return {"from_env": "dev", "to_env": "staging", "surface": surface,
            "artifact": artifact, "evidence_ids": evidence_ids,
            "recorded_at": recorded_at, "expires_at": recorded_at + 3600}


def _write_store(tmp_path, data):
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(data))
    return p


def _emit(store, run="run-42"):
    env = dict(os.environ, GATES_STORE=str(store))
    env.pop("FFS_ENV_REGISTRY_REQUIRED", None)
    return subprocess.run(["bash", str(EMIT), run], capture_output=True,
                          text=True, cwd=ROOT, env=env, timeout=60)


# ── INT-002 ─────────────────────────────────────────────────────────────────

def test_emit_single_staging_grant(tmp_path):
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    line = lines[0]
    assert "promote" in line
    assert "run-42" in line
    # verbatim gates.py:2885-2897 flag set, run_id positional
    assert "--from staging --to prod" in line
    assert "--surface 'web'" in line
    assert f"--artifact '{DIG_A}'" in line
    assert "--evidence 'gate-1'" in line
    assert "--ttl-hours" not in line  # default TTL is the promote arm's own


def test_emit_never_runs_store_byte_identical(tmp_path):
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    store = _write_store(tmp_path, data)
    before = store.read_bytes()
    r = _emit(store)
    assert r.returncode == 0
    assert store.read_bytes() == before  # cmp: EMIT-not-run proof
    after = json.loads(store.read_text())
    assert "_promotions" not in after
    assert "pending" not in after.get("_autonomy", {}).get("run-42", {})
    # nothing was promoted: the prod check-grant against the SAME store
    # still refuses
    env = dict(os.environ, GATES_STORE=str(store))
    env.pop("FFS_ENV_REGISTRY_REQUIRED", None)
    chk = subprocess.run(
        ["python3", str(GATES), "check-grant", "run-42",
         "--action", "deploy:prod-web"],
        capture_output=True, text=True, cwd=ROOT, env=env, timeout=60)
    assert chk.returncode != 0


def test_no_staging_grant_silent_exit_zero(tmp_path):
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["push:origin/main"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0
    assert r.stdout == ""
    assert r.stderr == ""
    # a run with no grants at all is silent too
    r2 = _emit(store, run="run-43")
    assert r2.returncode == 0 and r2.stdout == "" and r2.stderr == ""


def test_grant_without_evidence_one_stderr_advisory(tmp_path):
    data = {"gate-1": _gate(DIG_A, exit_code=1),      # failing gate
            "gate-2": _gate(None),                    # no artifact bound
            "gate-3": _gate(DIG_A, executed_by="caller")}  # fabricable
    data["_autonomy"] = _grants(["deploy:staging-web"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0
    assert r.stdout == ""
    advisory = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(advisory) == 1
    # value-free: no artifact digest bytes in the advisory
    assert "a" * 64 not in r.stderr


def test_two_staging_grants_one_line_each(tmp_path):
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["deploy:staging-web", "deploy:staging-api"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert any("--surface 'web'" in ln for ln in lines)
    assert any("--surface 'api'" in ln for ln in lines)


def test_emission_binds_to_run_and_surface(tmp_path):
    # sig 156cf77a: run-77's promotion records claim gate-b/DIG_B; run-42's
    # emit must fall back to the UNCLAIMED evidence only — DIG_B never appears.
    data = {"gate-a": _gate(DIG_A), "gate-b": _gate(DIG_B)}
    auto = _grants(["deploy:staging-web"], run="run-42")
    auto.update(_grants(["deploy:staging-web"], run="run-77"))
    data["_autonomy"] = auto
    data["_promotions"] = {"run-77": [_promo("web", DIG_B, ["gate-b"],
                                            time.time())]}
    store = _write_store(tmp_path, data)
    r = _emit(store, run="run-42")
    assert r.returncode == 0, r.stderr
    assert DIG_A in r.stdout
    assert DIG_B not in r.stdout
    # and run-77 emits its OWN promotion-bound artifact
    r77 = _emit(store, run="run-77")
    assert r77.returncode == 0, r77.stderr
    assert DIG_B in r77.stdout
    assert DIG_A not in r77.stdout


def test_multiple_artifacts_latest_by_ts_wins_and_skipped_named(tmp_path):
    # sig 7cec17e8 (promotion path): two records for the SAME run+surface —
    # the later recorded_at wins; the stale artifact is NAMED on stderr.
    t = time.time()
    data = {"gate-a": _gate(DIG_A), "gate-b": _gate(DIG_B)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    data["_promotions"] = {"run-42": [
        _promo("web", DIG_A, ["gate-a"], t - 100),
        _promo("web", DIG_B, ["gate-b"], t),
    ]}
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert DIG_B in lines[0]
    assert DIG_A not in r.stdout
    assert DIG_A in r.stderr  # skipped candidate named — auditable


def test_multiple_artifacts_fallback_insertion_order(tmp_path):
    # sig 7cec17e8 (fallback path): no promotion binding — the store's append
    # order is the only ts the schema has; the LAST-appended artifact wins.
    data = {"gate-a": _gate(DIG_A), "gate-b": _gate(DIG_B)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert DIG_B in lines[0]
    assert DIG_A in r.stderr  # skipped candidate named


def test_injection_proof_metachar_artifact_refused(tmp_path):
    # sig 31a608dc: a shell-metachar artifact value in the store → typed
    # refusal, NOTHING emitted, no metachar bytes on stdout.
    evil = "x; rm -rf /tmp/pwn @sha256:" + "c" * 64
    data = {"gate-1": _gate(evil)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "rm -rf" not in r.stdout + r.stderr  # value-free refusal
    assert "PROMOTE-EMIT-REFUSED" in r.stderr


def test_bad_run_id_refused():
    r = subprocess.run(["bash", str(EMIT), "run-42; echo pwn"],
                       capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "pwn" not in r.stdout
