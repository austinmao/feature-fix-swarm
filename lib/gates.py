"""Machine gates for human-out-of-loop runs (v4 hardening).

Completion authority lives HERE, not in agent self-report:
- gate evidence (exit codes + test counts) is the only thing that lets a
  task checkbox flip to [X]
- RED proofs must show a real failure before a GREEN task unlocks
- diffs are scanned for reward-hacking moves (deleted asserts, skips,
  exit 0, CI edits)
- spec/tasks coherence is checked before /feature-implement starts

Usage (inline heredoc in SKILL.md):
    python3 lib/gates.py run-gate     T042 -- pytest -q     # PREFERRED: executes
    python3 lib/gates.py run-red      T041 -- pytest tests/test_new.py
    python3 lib/gates.py record-gate  T042 --exit 0 --cmd "pytest -q" \
        --before "6 passed" --after "8 passed"   # trusted-caller only
    python3 lib/gates.py verify-done  T042        # exit 0 iff evidence OK
    python3 lib/gates.py verify-done  T042 --strict   # also reject caller-recorded
                                                  # evidence (or GATES_STRICT=1)
    python3 lib/gates.py phase-score  T040 T041 T042  # truth score from evidence;
                                                  # exit 1 if < 0.95 → rollback
    python3 lib/gates.py note-refuted T042 --reason "cause not reproducible at HEAD"
                                                  # zero-diff close; skips escalation
    python3 lib/gates.py confirm-refuted T042     # after review-gate refute-or-promote
                                                  # survives; unlocks strict verify-done
    python3 lib/gates.py note-failure T042 --sig "AssertionError foo.py:12"
                                                  # exit 1 when stuck (same sig 2x)
    python3 lib/gates.py proof run-42 T040 T041 --defer "live-send: no bot" \
                                                  # write proof-run-42.json;
                                                  # exit 1 on no-go verdict
    python3 lib/gates.py grant run-42 --action push:origin/main \
        --action merge:pr --ttl-hours 12   # operator pre-approval ledger
    python3 lib/gates.py check-grant run-42 --action push:origin/main
                                                  # exit 0 iff granted+unexpired
    python3 lib/gates.py pending run-42 --action rotate:secret --reason "…"
                                                  # unlisted gate → durable record
    python3 lib/gates.py pending run-42           # list; exit 1 if any pending
    python3 lib/gates.py preflight specs/NNN/preflight.json --run run-42
                                                  # env+probe checks; fail closed
    python3 lib/gates.py check-preflight run-42   # exit 0 iff recorded fresh pass
    python3 lib/gates.py record-red   T041 --exit 1 < red-run.log
    python3 lib/gates.py check-red    T041        # exit 0 iff RED proven
    python3 lib/gates.py scan-tamper  < diff.txt  # exit 1 + findings if hacked
    python3 lib/gates.py analyze SPEC_FILE TASKS_FILE

Evidence store path: $GATES_STORE (default .feature-fix-swarm/evidence.json).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Cheap deterministic gates run before expensive behavioral/LLM gates
# (fast/slow loop). A rung failure skips all later rungs and retries the task.
GATE_LADDER = ["compile", "typecheck", "lint", "unit", "integration", "e2e", "review"]

# Truth-score weights (ruflo Truth Verification System weighting).
TRUTH_WEIGHTS = {"compile": 0.35, "tests": 0.25, "lint": 0.20, "typecheck": 0.20}
TRUTH_THRESHOLD = 0.95  # strict mode: below this after max retries → rollback

FAILURE_MARKERS = re.compile(r"\bfailed\b|\berror\b|\bFAIL(ED)?\b|\bERROR\b|\bAssertionError\b")

TEST_FILE_PAT = re.compile(r"(^|/)(tests?/|test_[^/]+$|[^/]+[._-]test\.[a-z]+$|[^/]+\.spec\.[a-z]+$)")
CI_FILE_PAT = re.compile(r"\.github/workflows/|\.gitlab-ci|Jenkinsfile|\.circleci/")


# ── Stream A: completion authority ───────────────────────────────────────────

def _load_store(store: Path) -> dict:
    if not Path(store).exists():
        return {}
    with open(store) as f:
        return json.load(f)


def _save_store(store: Path, data: dict) -> None:
    # atomic: write a temp file in the same dir, then rename over the store —
    # a crash or parallel reader never sees a torn file.
    store = Path(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=store.parent, prefix=".evidence-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, store)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class _StoreLock:
    """Advisory flock serializing read-modify-write across parallel tasks."""

    def __init__(self, store: Path):
        self.path = Path(store).with_suffix(".lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.path, "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def record_gate_evidence(store: Path, task_id: str, *, exit_code: int, cmd: str,
                         tests_before: str = "", tests_after: str = "") -> None:
    """Trusted-caller write. Prefer run_gate(), which executes the command
    itself and records the REAL exit code — an agent cannot fabricate it."""
    with _StoreLock(store):
        data = _load_store(store)
        entry = data.setdefault(task_id, {})
        entry["gate"] = {
            "exit_code": exit_code,
            "cmd": cmd,
            "tests_before": tests_before,
            "tests_after": tests_after,
            "executed_by": "caller",
        }
        _save_store(store, data)


def run_gate(store: Path, task_id: str, cmd: list[str], timeout: int = 1800) -> int:
    """Execute the gate command and record the REAL exit code (P1: evidence
    bound to the runner, not caller-supplied --exit). Returns the exit code."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    full = proc.stdout + proc.stderr
    tail = full[-2000:]
    lines = tail.splitlines()
    # Failure signature = the DISCRIMINATING failing lines, not the final
    # summary line — different failures share "1 failed in ..." (codex gate
    # round 2, v3.14). Scan the FULL output (round 3: truncating first drops
    # early traceback lines); last 3 marker lines, else last line as fallback.
    marker_lines = [ln for ln in full.splitlines() if FAILURE_MARKERS.search(ln)]
    failure_sig = " | ".join(marker_lines[-3:])[:400] if proc.returncode != 0 else ""
    with _StoreLock(store):
        data = _load_store(store)
        entry = data.setdefault(task_id, {})
        entry["gate"] = {
            "exit_code": proc.returncode,
            "cmd": " ".join(cmd),
            "tests_before": "",
            "tests_after": lines[-1] if lines else "",
            "failure_sig": failure_sig or (lines[-1] if lines and proc.returncode != 0 else ""),
            "executed_by": "run_gate",
        }
        _save_store(store, data)
    return proc.returncode


def run_red(store: Path, task_id: str, cmd: list[str], timeout: int = 1800) -> bool:
    """Execute the RED test command; store a proof only if it REALLY failed
    (nonzero exit from the runner itself). Returns True iff RED proven."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode == 0:
        return False
    tail = (proc.stdout + proc.stderr)[-2000:]
    with _StoreLock(store):
        data = _load_store(store)
        entry = data.setdefault(task_id, {})
        entry["red_proof"] = {"exit_code": proc.returncode, "log_tail": tail,
                              "executed_by": "run_red"}
        _save_store(store, data)
    return True


def verify_done(store: Path, task_id: str, strict: bool = False) -> bool:
    """A task is done ONLY if recorded gate evidence exists with exit 0.
    strict=True additionally requires the evidence was produced by the
    runner itself (run_gate) — caller-recorded evidence is rejected because
    a shell-capable agent can fabricate `record-gate --exit 0`."""
    entry = _load_store(store).get(task_id, {})
    refuted = entry.get("refuted")
    if refuted:
        # REFUTED (v3.17.0): diagnosis proven wrong at HEAD — the task closes
        # with a zero diff. A refutation is a judgment call the runner cannot
        # execute, so under strict it must be CONFIRMED (confirm-refuted,
        # recorded only after review-gate refute-or-promote survives) — a
        # bare caller-asserted note must not satisfy GATES_STRICT=1
        # (codex v3.17 gate, CRITICAL).
        return bool(refuted.get("confirmed")) if strict else True
    gate = entry.get("gate")
    if not (bool(gate) and gate.get("exit_code") == 0):
        return False
    if strict and gate.get("executed_by") != "run_gate":
        return False
    return True


def append_result(log: Path, line: str) -> None:
    """Append-only run log — history is never rewritten."""
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(line.rstrip("\n") + "\n")


# ── Stream B: RED proof ──────────────────────────────────────────────────────

def record_red_proof(store: Path, task_id: str, log_text: str, *, exit_code: int) -> bool:
    """Store a RED proof iff the run actually failed (nonzero exit AND
    failure markers in output). An all-green log is not a RED proof."""
    if exit_code == 0 or not FAILURE_MARKERS.search(log_text):
        return False
    with _StoreLock(store):
        data = _load_store(store)
        entry = data.setdefault(task_id, {})
        entry["red_proof"] = {"exit_code": exit_code, "log_tail": log_text[-2000:]}
        _save_store(store, data)
    return True


def check_red(store: Path, task_id: str) -> bool:
    return "red_proof" in _load_store(store).get(task_id, {})


# ── Stream C: truth score ────────────────────────────────────────────────────

def truth_score(compile_ok: bool, tests_ok: bool, lint_ok: bool, typecheck_ok: bool) -> float:
    score = 0.0
    score += TRUTH_WEIGHTS["compile"] if compile_ok else 0.0
    score += TRUTH_WEIGHTS["tests"] if tests_ok else 0.0
    score += TRUTH_WEIGHTS["lint"] if lint_ok else 0.0
    score += TRUTH_WEIGHTS["typecheck"] if typecheck_ok else 0.0
    return round(score, 2)


# Gate-command → truth-score category. Order matters: specific tools first,
# "tests" is the catch-all default (unknown gate = behavioral evidence).
_CATEGORY_PATTERNS = [
    ("typecheck", re.compile(r"\btsc\b|mypy|pyright|typecheck")),
    ("lint", re.compile(r"\blint\b|ruff|eslint|flake8|shellcheck|clippy")),
    ("compile", re.compile(r"\bbuild\b|\bcompile\b|\bmake\b")),
    ("tests", re.compile(r"pytest|vitest|jest|go test|cargo test|bats|npm test")),
]


def classify_gate_cmd(cmd: str) -> str:
    for cat, pat in _CATEGORY_PATTERNS:
        if pat.search(cmd):
            return cat
    return "tests"


def phase_score(store: Path, task_ids: list[str], strict: bool = False) -> tuple[float, dict]:
    """Aggregate truth score over a phase's tasks from STORED evidence
    (wires the previously-dead truth_score into the pipeline).
    - each task's gate cmd is classified into a truth category
    - a category is OK iff every one of its gates exited 0
    - score = weights of OK categories / weights of categories present
    - any task with NO gate evidence at all → 0.0 (unproven phase)
    - strict: caller-recorded evidence counts as MISSING — phase-score is the
      rollback authority, so it must honor the same provenance boundary as
      verify_done (codex gate round 1, v3.14)."""
    data = _load_store(store)

    def _usable(t: str) -> bool:
        gate = data.get(t, {}).get("gate")
        if not gate:
            return False
        if strict and gate.get("executed_by") != "run_gate":
            return False
        return True

    missing = [t for t in task_ids if not _usable(t)]
    if missing:
        return 0.0, {"missing": missing}
    cats: dict[str, bool] = {}
    for t in task_ids:
        gate = data[t]["gate"]
        cat = classify_gate_cmd(gate.get("cmd", ""))
        cats[cat] = cats.get(cat, True) and gate.get("exit_code") == 0
    present = sum(TRUTH_WEIGHTS[c] for c in cats)
    ok = sum(TRUTH_WEIGHTS[c] for c, is_ok in cats.items() if is_ok)
    return (round(ok / present, 2) if present else 0.0), cats


# ── Stream D: reward-hacking guards ──────────────────────────────────────────

def scan_test_tampering(diff_text: str) -> list[str]:
    """Flag reward-hacking moves in a unified diff. Empty list = clean."""
    findings: list[str] = []
    in_test_file = False
    for line in diff_text.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            path = line[4:]
            path = path[2:] if path.startswith(("a/", "b/")) else path
            in_test_file = bool(TEST_FILE_PAT.search(path))
            if line.startswith("+++ ") and CI_FILE_PAT.search(path):
                findings.append(f"CI/workflow file edited: {path}")
            continue
        if line.startswith("-") and not line.startswith("---"):
            # deletions of asserts are suspicious everywhere: in tests they
            # weaken the gate; in impl files they delete runtime invariants
            # (codex-gate round 2, PR #13).
            if re.search(r"\bassert\b|expect\(", line):
                kind = "test" if in_test_file else "source"
                findings.append(f"assert deleted from {kind} file: {line.strip()}")
        if line.startswith("+") and not line.startswith("+++"):
            if in_test_file and re.search(r"\bassert\s+(True|1)\b|expect\(\s*(true|1)\s*\)", line):
                findings.append(f"always-true (weakened) assertion added: {line.strip()}")
            if re.search(r"\.skip\b|@pytest\.mark\.skip|\bxfail\b|@unittest\.skip", line):
                findings.append(f"test skip added: {line.strip()}")
            if re.search(r"\bexit 0\b|sys\.exit\(0\)|process\.exit\(0\)", line):
                findings.append(f"unconditional exit 0 added: {line.strip()}")
    return findings


def check_test_separation(changed_files: list[str], task_kind: str) -> list[str]:
    """Implementation (GREEN) tasks may not touch test files — test edits
    belong to test-author (RED) tasks. Returns violating paths."""
    if task_kind != "impl":
        return []
    return [f for f in changed_files if TEST_FILE_PAT.search(f)]


# ── Stream E: no-progress detection ──────────────────────────────────────────

def no_progress(failure_signatures: list[str]) -> bool:
    """True when the last two failure signatures are identical — the loop is
    stuck; stop and report instead of burning retries."""
    return len(failure_signatures) >= 2 and failure_signatures[-1] == failure_signatures[-2]


def note_failure(store: Path, task_id: str, signature: str) -> bool:
    """Append a failure signature to the task's history and report whether
    the loop is stuck (same signature twice in a row). Wires no_progress
    into the retry loop via the evidence store.
    Blank/whitespace signatures are IGNORED (not recorded, never stuck) —
    two empty captures would otherwise stop the loop with a false
    NO-PROGRESS (codex gate round 2, v3.14)."""
    if not signature or not signature.strip():
        return False
    with _StoreLock(store):
        data = _load_store(store)
        sigs = data.setdefault(task_id, {}).setdefault("failure_sigs", [])
        sigs.append(signature)
        _save_store(store, data)
    return no_progress(sigs)


def note_refuted(store: Path, task_id: str, reason: str) -> bool:
    """Record a REFUTED outcome (v3.17.0, ported from the
    fable-agent-orchestration result-state vocabulary): the task's diagnosis
    was checked against current HEAD and found wrong, so NOTHING ships. This
    is a result, not a failure — it does not enter the failure-signature
    history and must NOT trigger the escalation ladder. A reason is
    mandatory; a blank refutation is a dodge, not a finding."""
    if not reason or not reason.strip():
        return False
    with _StoreLock(store):
        data = _load_store(store)
        data.setdefault(task_id, {})["refuted"] = {"reason": reason.strip(),
                                                   "executed_by": "caller",
                                                   "confirmed": False}
        _save_store(store, data)
    return True


def confirm_refuted(store: Path, task_id: str) -> bool:
    """Second step of the refutation protocol: recorded ONLY after the
    refutation survives review-gate refute-or-promote. Unlocks strict
    verify-done. Still caller-executed (no runner-provable refutation
    exists) — the two-step split makes skipping the adversarial check a
    distinct, auditable action rather than the default path."""
    with _StoreLock(store):
        data = _load_store(store)
        refuted = data.get(task_id, {}).get("refuted")
        if not refuted:
            return False
        refuted["confirmed"] = True
        _save_store(store, data)
    return True


def sanitize_reason(reason: str) -> str:
    """Strip control chars (ANSI escapes, newlines) so a crafted stored
    reason cannot spoof gate output lines in logs (codex v3.17, MEDIUM)."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", reason)[:200]


def proof_artifact(store: Path, run_id: str, task_ids: list[str],
                   strict: bool = False, deferrals: list[str] | None = None,
                   residuals_text: str | None = None) -> dict:
    """Per-run proof artifact (ported from the openclaw evidence discipline):
    one claim per task with the evidence command, real exit code, a sha256 of
    the stored log material, and a live-vs-structural kind (runner-executed vs
    caller-recorded). Verdict is go ONLY when every claim has exit 0 and — in
    strict mode — is runner-proven. Deferrals are NAMED in the artifact, never
    silently passed."""
    data = _load_store(store)
    claims: list[dict] = []
    missing: list[str] = []
    # repeated ids overstate coverage (a wrapper can drop one task and
    # duplicate another while the artifact still looks complete) — surface
    # and fail (codex round 8 P2)
    seen: set[str] = set()
    duplicates = sorted({t for t in task_ids if t in seen or seen.add(t)})
    task_ids = list(dict.fromkeys(task_ids))
    for task_id in task_ids:
        gate = data.get(task_id, {}).get("gate")
        if not gate:
            missing.append(task_id)
            continue
        live = gate.get("executed_by") in ("run_gate", "run_red")
        log_material = (gate.get("tests_after", "") + "\n" +
                        gate.get("failure_sig", "")).encode()
        claims.append({
            "task_id": task_id,
            "claim": f"gate passed for {task_id}" if gate.get("exit_code") == 0
                     else f"gate FAILED for {task_id}",
            "evidence_cmd": gate.get("cmd", ""),
            "exit_code": gate.get("exit_code"),
            "kind": "live" if live else "structural",
            "log_sha256": hashlib.sha256(log_material).hexdigest(),
        })
    # A deferral named on argv but absent from residuals.md is a silent pass —
    # verify the companion record when residuals_text is provided (codex v3.15
    # round 2 P2). The deferral's name (text before ':') must appear there.
    unrecorded: list[str] = []
    if residuals_text is not None:
        # Structural parse of the residual record format
        #   - [ ] {date} {name}: {reason} (run {run_id})
        # — free-form mentions of the name inside another record's reason must
        # not count (round 5 P2), only entries for THIS run count (round 4 P1),
        # and the name field is compared exactly, never by substring (round 3 P1).
        recorded_names: set[str] = set()
        for ln in residuals_text.splitlines():
            # unchecked form only — a closed '- [x]' item is a RESOLVED
            # residual, not a live deferral record (round 7 P2)
            m = re.match(r"^\s*-\s*\[ \]\s+(?P<field>[^:]+):.*\(run\s+"
                         + re.escape(run_id) + r"\)\s*$", ln)
            if not m:
                continue
            field = m.group("field").strip()
            # optional leading date token
            field = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", field)
            recorded_names.add(field)
        for d in deferrals or []:
            name = d.split(":", 1)[0].strip()
            # blank/degenerate names are invalid, not silently OK (round 5 P2)
            if not name or name not in recorded_names:
                unrecorded.append(d)
        # reverse direction (round 6 P1): a residual recorded for THIS run but
        # not echoed via --defer would let the artifact claim go while
        # residuals.md records live risk — surface it and fail the verdict.
        argv_names = {d.split(":", 1)[0].strip() for d in deferrals or []}
        unechoed = sorted(recorded_names - argv_names)
    else:
        unechoed = []
    # claims must be non-empty: all([]) is True, so an empty proof would
    # otherwise read as go (codex v3.15 round 1 P1).
    go = (bool(claims)
          and not missing
          and not duplicates
          and not unrecorded
          and not unechoed
          and all(c["exit_code"] == 0 for c in claims)
          and (not strict or all(c["kind"] == "live" for c in claims)))
    return {
        "run_id": run_id,
        "verdict": "go" if go else "no-go",
        "strict": strict,
        "claims": claims,
        "missing": missing,
        "duplicate_task_ids": duplicates,
        "deferrals": list(deferrals or []),
        "unrecorded_deferrals": unrecorded,
        "unechoed_residuals": unechoed,
    }


# ── Stream G: spec/tasks coherence ───────────────────────────────────────────

def analyze_artifacts(spec_text: str, tasks_text: str) -> list[str]:
    """Cross-artifact consistency gate (spec-kit analyze analog).
    Every spec story needs tasks; every task story must exist in the spec;
    every phase needs a review-gate task; every story phase needs an e2e task."""
    findings: list[str] = []
    spec_stories = set(re.findall(r"\bUS(\d+)\b", spec_text))

    phases: dict[str, list[str]] = {}
    current = "(no phase)"
    for line in tasks_text.splitlines():
        if line.startswith("## Phase"):
            current = line.strip()
            phases[current] = []
        elif re.match(r"- \[[ XxFfSs]\] ", line):
            phases.setdefault(current, []).append(line)

    task_stories = set(re.findall(r"\[US(\d+)\]", tasks_text))
    for us in sorted(task_stories - spec_stories):
        findings.append(f"tasks reference US{us} which is not in the spec")
    for us in sorted(spec_stories - task_stories):
        findings.append(f"spec story US{us} has no tasks")

    for phase, lines in phases.items():
        if not lines:
            continue
        if not any("[qa:review-gate]" in ln for ln in lines):
            findings.append(f"{phase}: no review-gate task (phase gate missing)")
        # a story phase is one whose TASKS carry [USn] tags — header wording
        # varies ("## Phase 3: User Story 1 — ..." has no USn token), so never
        # key the e2e rule on the header (codex-gate round 2, PR #13).
        is_story_phase = any(re.search(r"\[US\d+\]", ln) for ln in lines) or \
            re.search(r"US\d+|User Story", phase)
        if is_story_phase and not any(
                re.search(r"\[qa:[a-z0-9,-]*e2e", ln) for ln in lines):
            findings.append(f"{phase}: story phase has no e2e smoke task")
    return findings


# ── Stream H: autonomy grant ledger + preflight (v3.18.0) ────────────────────
# Front-load run-time decisions to plan-time so unattended runs never stall:
# the operator approves a TYPED action list once (grant), the loop checks the
# ledger mechanically (check-grant), unlisted gates are recorded for morning
# resume (pending), and env/service requirements are proven reachable BEFORE
# the run starts (preflight). Fail closed everywhere.

ACTION_PAT = re.compile(r"^[a-z][a-z0-9_-]*:\S.*$")
GRANT_DEFAULT_TTL_HOURS = 24.0


def _now() -> float:
    import time
    return time.time()


def grant_actions(store: Path, run_id: str, actions: list[str], *,
                  ttl_hours: float = GRANT_DEFAULT_TTL_HOURS,
                  granted_by: str = "operator") -> bool:
    """Record operator-approved actions for a run. Actions must be typed
    ('type:target', e.g. 'push:origin/main') — free prose never matches at
    run time, so it is rejected here rather than silently failing at 3am."""
    if not actions or any(not ACTION_PAT.match(a) for a in actions):
        return False
    granted_at = _now()
    with _StoreLock(store):
        data = _load_store(store)
        auto = data.setdefault("_autonomy", {}).setdefault(run_id, {})
        grants = auto.setdefault("grants", {})
        for a in actions:
            grants[a] = {
                "granted_at": granted_at,
                "expires_at": granted_at + ttl_hours * 3600,
                "granted_by": granted_by,
            }
        # a grant resolves any matching pending record
        auto["pending"] = [p for p in auto.get("pending", [])
                           if p["action"] not in grants]
        _save_store(store, data)
    return True


def check_grant(store: Path, run_id: str, action: str,
                now: float | None = None) -> bool:
    """Exit-code authority for the autonomous loop: True ONLY for an exact,
    unexpired, run-bound grant. Everything else fails closed."""
    entry = (_load_store(store).get("_autonomy", {})
             .get(run_id, {}).get("grants", {}).get(action))
    if not entry:
        return False
    return (now if now is not None else _now()) < entry.get("expires_at", 0)


def record_pending(store: Path, run_id: str, action: str, reason: str) -> None:
    """An unlisted gate hit mid-run: STOP, but leave a durable record so the
    morning resume is one `grant` command (long-run-continuity port)."""
    with _StoreLock(store):
        data = _load_store(store)
        auto = data.setdefault("_autonomy", {}).setdefault(run_id, {})
        pending = auto.setdefault("pending", [])
        if not any(p["action"] == action for p in pending):
            pending.append({"action": action,
                            "reason": sanitize_reason(reason),
                            "recorded_at": _now()})
        _save_store(store, data)


def list_pending(store: Path, run_id: str) -> list[dict]:
    return list(_load_store(store).get("_autonomy", {})
                .get(run_id, {}).get("pending", []))


def preflight_check(requirements: list[dict], timeout: int = 30) -> dict:
    """Prove env vars present and services reachable BEFORE an unattended run.
    Env checks report presence only — a secret value never enters the result.
    Probes execute for real (exit 0 = reachable). Empty manifest fails: an
    unattended run with nothing declared is undeclared, not requirement-free."""
    results: list[dict] = []
    for req in requirements:
        kind, name = req.get("kind"), req.get("name", "")
        if kind == "env":
            ok = bool(os.environ.get(name))
            detail = "present" if ok else "MISSING"
        elif kind == "probe":
            try:
                proc = subprocess.run(req.get("cmd", "false"), shell=True,
                                      capture_output=True, timeout=timeout)
                ok = proc.returncode == 0
                detail = f"exit {proc.returncode}"
            except subprocess.TimeoutExpired:
                ok, detail = False, f"timeout after {timeout}s"
        else:
            ok, detail = False, f"unknown kind: {kind}"
        results.append({"kind": kind, "name": name, "ok": ok, "detail": detail})
    return {"pass": bool(results) and all(r["ok"] for r in results),
            "results": results, "checked_at": _now()}


def record_preflight(store: Path, run_id: str, result: dict) -> None:
    with _StoreLock(store):
        data = _load_store(store)
        data.setdefault("_autonomy", {}).setdefault(run_id, {})["preflight"] = {
            "pass": result["pass"], "checked_at": result["checked_at"],
            "results": result["results"]}
        _save_store(store, data)


def check_preflight(store: Path, run_id: str, *, max_age_hours: float = 24.0,
                    now: float | None = None) -> bool:
    """True ONLY for a recorded PASSING preflight fresh enough to trust."""
    pf = (_load_store(store).get("_autonomy", {})
          .get(run_id, {}).get("preflight"))
    if not pf or not pf.get("pass"):
        return False
    age = (now if now is not None else _now()) - pf.get("checked_at", 0)
    return age < max_age_hours * 3600


# ── CLI ──────────────────────────────────────────────────────────────────────

def _store_path() -> Path:
    return Path(os.environ.get("GATES_STORE", ".feature-fix-swarm/evidence.json"))


def _flag(args: list[str], name: str, default: str = "") -> str:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
    return default


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    store = _store_path()
    if cmd == "record-gate":
        print("WARNING: trusted-caller evidence (executed_by=caller); prefer "
              "run-gate — exit codes recorded here are not runner-verified",
              file=sys.stderr)
        record_gate_evidence(store, args[0], exit_code=int(_flag(args, "--exit", "1")),
                             cmd=_flag(args, "--cmd"), tests_before=_flag(args, "--before"),
                             tests_after=_flag(args, "--after"))
        return 0
    if cmd == "verify-done":
        strict = "--strict" in args or os.environ.get("GATES_STRICT") == "1"
        gate = _load_store(store).get(args[0], {}).get("gate") or {}
        by = gate.get("executed_by", "unknown")
        ok = verify_done(store, args[0], strict=strict)
        refuted = _load_store(store).get(args[0], {}).get("refuted")
        if ok and refuted:
            print(f"DONE-REFUTED: zero-diff close ({sanitize_reason(refuted['reason'])})")
        elif refuted and strict and not refuted.get("confirmed"):
            print(f"NOT-DONE: refutation unconfirmed for {args[0]} — run "
                  "review-gate refute-or-promote, then gates.py confirm-refuted")
        elif ok:
            print(f"DONE-VERIFIED (executed_by={by})")
        elif strict and gate.get("exit_code") == 0:
            print(f"NOT-DONE: evidence not runner-executed (executed_by={by}); "
                  f"re-run the gate via run-gate for {args[0]}")
        else:
            print(f"NOT-DONE: no passing gate evidence for {args[0]}")
        return 0 if ok else 1
    if cmd == "run-gate":
        sep = args.index("--") if "--" in args else 1
        rc = run_gate(store, args[0], args[sep + 1:] if "--" in args else args[1:])
        print(f"GATE-EXIT {rc}")
        return rc
    if cmd == "run-red":
        sep = args.index("--") if "--" in args else 1
        ok = run_red(store, args[0], args[sep + 1:] if "--" in args else args[1:])
        print("RED-RECORDED" if ok else "NOT-RED: command passed — not a failing test")
        return 0 if ok else 1
    if cmd == "record-red":
        print("WARNING: trusted-caller RED proof; prefer run-red — the exit "
              "code here is not runner-verified", file=sys.stderr)
        ok = record_red_proof(store, args[0], sys.stdin.read(),
                              exit_code=int(_flag(args, "--exit", "0")))
        print("RED-RECORDED" if ok else "NOT-RED: log shows no real failure")
        return 0 if ok else 1
    if cmd == "check-red":
        ok = check_red(store, args[0])
        print("RED-PROVEN" if ok else f"NO-RED-PROOF: write the failing test first ({args[0]})")
        return 0 if ok else 1
    if cmd == "scan-tamper":
        findings = scan_test_tampering(sys.stdin.read())
        for f in findings:
            print(f"TAMPER: {f}")
        return 1 if findings else 0
    if cmd == "phase-score":
        task_ids = [a for a in args if not a.startswith("--")]
        strict = "--strict" in args or os.environ.get("GATES_STRICT") == "1"
        score, breakdown = phase_score(store, task_ids, strict=strict)
        threshold = float(os.environ.get("TRUTH_THRESHOLD", TRUTH_THRESHOLD))
        for cat, val in breakdown.items():
            print(f"  {cat}: {'OK' if val is True else val if cat == 'missing' else 'FAIL'}")
        print(f"TRUTH-SCORE {score:.2f} (threshold {threshold})")
        if score < threshold:
            print("BELOW-THRESHOLD: roll back to phase-start checkpoint and re-plan")
            return 1
        return 0
    if cmd == "note-refuted":
        ok = note_refuted(store, args[0], _flag(args, "--reason"))
        print("REFUTED-RECORDED: task closes with zero diff; route the refutation "
              "through review-gate before flipping the checkbox" if ok else
              "NO-REASON: a refutation must name why the diagnosis is wrong")
        return 0 if ok else 1
    if cmd == "confirm-refuted":
        print("WARNING: trusted-caller confirmation — run this ONLY after the "
              "refutation survived review-gate refute-or-promote", file=sys.stderr)
        ok = confirm_refuted(store, args[0])
        print("REFUTED-CONFIRMED: strict verify-done unlocked" if ok else
              f"NO-REFUTATION: nothing recorded for {args[0]} — note-refuted first")
        return 0 if ok else 1
    if cmd == "note-failure":
        stuck = note_failure(store, args[0], _flag(args, "--sig"))
        print("NO-PROGRESS: same failure signature twice in a row — stop and report"
              if stuck else "PROGRESS-OK")
        return 1 if stuck else 0
    if cmd == "proof":
        # argparse fails closed on every malformed shape the hand parser kept
        # leaking (codex rounds 1-4): missing run id, trailing/optionless
        # --defer/--out, unknown flags (--stric typo, -h), option-as-value.
        parser = argparse.ArgumentParser(prog="gates.py proof", add_help=False,
                                         allow_abbrev=False)
        parser.add_argument("run_id")
        parser.add_argument("task_ids", nargs="*")
        parser.add_argument("--defer", action="append", dest="deferrals",
                            default=[], metavar="'name: reason'")
        parser.add_argument("--out")
        parser.add_argument("--strict", action="store_true")
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return 2
        run_id, task_ids, deferrals = ns.run_id, ns.task_ids, ns.deferrals
        strict = ns.strict or os.environ.get("GATES_STRICT") == "1"
        residuals_file = store.parent / "residuals.md"
        residuals_text = residuals_file.read_text() if residuals_file.exists() else ""
        art = proof_artifact(store, run_id, task_ids, strict=strict,
                             deferrals=deferrals, residuals_text=residuals_text)
        # run_id is argv-controlled: sanitize before composing the default
        # artifact filename so '../x' can't escape the store dir (round 2 P2).
        # When sanitization changed the id, append a short hash of the ORIGINAL
        # so distinct unsafe ids ('a/b' vs 'a?b') can't collapse onto one
        # filename and silently overwrite each other (round 7 P2).
        safe_run = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)
        if safe_run != run_id:
            safe_run += "-" + hashlib.sha256(run_id.encode()).hexdigest()[:8]
        out = Path(ns.out or store.parent / f"proof-{safe_run}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        # temp-file + rename: never follow a pre-planted symlink at the
        # predictable artifact path (codex round 6 P2) — os.replace swaps the
        # symlink itself, the target is never written through.
        fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".proof-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(art, indent=2) + "\n")
            os.replace(tmp, out)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        print(f"PROOF-{art['verdict'].upper()}: {out}")
        for d in art["deferrals"]:
            print(f"  DEFERRED: {d}")
        for d in art["unrecorded_deferrals"]:
            print(f"  DEFERRAL-UNRECORDED (add to {residuals_file}): {d}")
        for d in art["unechoed_residuals"]:
            print(f"  RESIDUAL-UNECHOED (pass --defer '{d}: …'): {d}")
        for m in art["missing"]:
            print(f"  MISSING-EVIDENCE: {m}")
        return 0 if art["verdict"] == "go" else 1
    if cmd == "grant":
        run_id = args[0]
        actions = [args[i + 1] for i, a in enumerate(args) if a == "--action"]
        ttl = float(_flag(args, "--ttl-hours", str(GRANT_DEFAULT_TTL_HOURS)))
        if not grant_actions(store, run_id, actions, ttl_hours=ttl):
            print("GRANT-REJECTED: actions must be typed 'type:target' "
                  "(e.g. push:origin/main) and non-empty")
            return 1
        for a in actions:
            print(f"GRANTED: {a} (run {run_id}, ttl {ttl}h)")
        return 0
    if cmd == "check-grant":
        run_id, action = args[0], _flag(args, "--action")
        if check_grant(store, run_id, action):
            print(f"GRANTED: {action}")
            return 0
        print(f"NOT-GRANTED: {action} (run {run_id}) — record with "
              f"`gates.py pending {run_id} --action '{action}' --reason …`, "
              f"STOP, and wait for operator grant")
        return 1
    if cmd == "pending":
        run_id = args[0]
        action = _flag(args, "--action")
        if action:
            record_pending(store, run_id, action, _flag(args, "--reason"))
            print(f"PENDING-RECORDED: {action} (run {run_id})")
            return 0
        pend = list_pending(store, run_id)
        for p in pend:
            print(f"PENDING: {p['action']} — {p['reason']}")
        print("NO-PENDING" if not pend else
              f"PENDING-COUNT: {len(pend)} — approve via `gates.py grant "
              f"{run_id} --action <action>` then resume")
        return 1 if pend else 0
    if cmd == "preflight":
        manifest = Path(args[0])
        run_id = _flag(args, "--run", "default")
        with open(manifest) as f:
            requirements = json.load(f)
        result = preflight_check(requirements)
        record_preflight(store, run_id, result)
        for r in result["results"]:
            mark = "ok" if r["ok"] else "FAIL"
            print(f"  [{mark}] {r['kind']}:{r['name']} — {r['detail']}")
        print("PREFLIGHT-PASS" if result["pass"] else
              "PREFLIGHT-FAIL: fix the failing requirements BEFORE starting "
              "an unattended run")
        return 0 if result["pass"] else 1
    if cmd == "check-preflight":
        run_id = args[0]
        if check_preflight(store, run_id):
            print(f"PREFLIGHT-OK: {run_id}")
            return 0
        print(f"PREFLIGHT-STALE-OR-FAILED: {run_id} — re-run "
              f"`gates.py preflight <manifest> --run {run_id}`")
        return 1
    if cmd == "analyze":
        with open(args[0]) as f:
            spec = f.read()
        with open(args[1]) as f:
            tasks = f.read()
        findings = analyze_artifacts(spec, tasks)
        for f in findings:
            print(f"ANALYZE: {f}")
        print("ANALYZE-PASS" if not findings else f"ANALYZE-FAIL: {len(findings)} findings")
        return 1 if findings else 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
