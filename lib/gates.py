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
    python3 lib/gates.py run-gate stg-web --artifact name@sha256:<64hex> -- pytest -q
                                                  # staging gate bound to artifact
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
    python3 lib/gates.py promote run-42 --from staging --to prod --surface web \
        --artifact name@sha256:<64hex> --evidence gate-id [--evidence gate-id2]
                                                  # record promote proof binding
                                                  # artifact identity to real
                                                  # recorded staging gate evidence
    python3 lib/gates.py check-grant run-42 --action deploy:prod-web \
        --artifact name@sha256:<64hex> [--manifest parity.json|parity.yaml]
                                                  # prod actions (deploy/flip/
                                                  # migrate:prod-*) ALSO require
                                                  # a fresh staging->prod promote
                                                  # record for this artifact
    python3 lib/gates.py pending run-42 --action rotate:secret --reason "…"
                                                  # unlisted gate → durable record
    python3 lib/gates.py pending run-42           # list; exit 1 if any pending
    python3 lib/gates.py preflight specs/NNN/preflight.json --run run-42
                                                  # env+probe checks; fail closed
    python3 lib/gates.py check-preflight run-42   # exit 0 iff recorded fresh pass
    python3 lib/gates.py record-red   T041 --exit 1 < red-run.log
    python3 lib/gates.py check-red    T041        # exit 0 iff RED proven
    python3 lib/gates.py scan-tamper  < diff.txt  # exit 1 + findings if hacked
    python3 lib/gates.py delegation-audit TRANSCRIPT.jsonl [--threshold 3]
                                                  # advisory (always exit 0):
                                                  # spawn-model histogram +
                                                  # UNPINNED-BUILD/INLINE-MECHANICAL
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


def run_gate(store: Path, task_id: str, cmd: list[str], timeout: int = 1800, *,
             artifact: str | None = None) -> int:
    """Execute the gate command and record the REAL exit code (P1: evidence
    bound to the runner, not caller-supplied --exit). When `artifact` is
    supplied, bind the runner-produced evidence to that immutable identity so
    it can later satisfy a promotion check. Returns the exit code."""
    if artifact is not None and not _valid_artifact(artifact):
        raise ValueError("run-gate artifact must be an immutable digest or commit sha")
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
        gate = {
            "exit_code": proc.returncode,
            "cmd": " ".join(cmd),
            "tests_before": "",
            "tests_after": lines[-1] if lines else "",
            "failure_sig": failure_sig or (lines[-1] if lines and proc.returncode != 0 else ""),
            "executed_by": "run_gate",
        }
        if artifact is not None:
            gate["artifact"] = artifact
        entry["gate"] = gate
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

# Web-surface file paths in task lines (codex rounds 3+4). Must stay aligned
# with scripts/browser-proof.sh WEB_RE — same web-touch contract at plan time
# as at QA time, else a hooks/-only or route.ts-only phase demands browser
# proof at QA with no scenarios.md to run. app/ + api/ are anchored to
# path-with-extension so prose mentions ("the api/ contract") don't match.
WEB_TASK_PATH_RE = re.compile(
    r"\.(tsx|jsx|vue|svelte|astro|html|css|scss|less)\b"
    r"|(?:^|[\s(`'\"/])(?:pages|routes|components|emails|templates|public"
    r"|hooks|stores?|styles?)/"
    r"|(?:^|[\s(`'\"/])(?:app|api)/\S*\.[a-z]+\b"
    r"|(?:^|[\s(`'\"])(?:tailwind|next|nuxt|vite|astro|svelte)\.config\.",
    re.MULTILINE)


def analyze_artifacts(spec_text: str, tasks_text: str,
                      has_scenarios: bool = False) -> list[str]:
    """Cross-artifact consistency gate (spec-kit analyze analog).
    Every spec story needs tasks; every task story must exist in the spec;
    every phase needs a review-gate task; every story phase needs an e2e task.
    has_scenarios=True (specs/NNN/scenarios.md exists → browser-touchable
    spec): every story phase must also carry a [qa:browser] runtime-proof
    gate task — otherwise proof enforcement is opt-in prose (v3.20 F1)."""
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
        browser_lines = [ln for ln in lines
                         if re.search(r"\[qa:[a-z0-9,-]*browser", ln)]
        if has_scenarios and is_story_phase and not browser_lines:
            findings.append(f"{phase}: browser-touchable spec (scenarios.md) "
                            "but no [qa:browser] runtime-proof gate task")
        for ln in browser_lines:
            # substring "runtime_proof" is spoofable by a placeholder mention
            # (codex round) — require the actual verify gate command
            if not re.search(r"runtime_proof\.py\s+verify\b", ln):
                findings.append(f"{phase}: [qa:browser] task lacks a "
                                "runtime_proof.py verify gate command")
    if not has_scenarios and re.search(r"\[qa:[a-z0-9,-]*browser", tasks_text):
        findings.append("tasks carry [qa:browser] but specs/NNN/scenarios.md "
                        "is missing — decompose must emit the BDD scenarios")
    # codex round 3: a browser-touching plan that omits scenarios.md entirely
    # must not slide through. Web-touch is detected from web-surface file
    # paths named in the tasks themselves (UI extensions, UI dirs, framework
    # configs — same signal family as scripts/browser-proof.sh WEB_RE).
    # False positives just ask for scenarios.md, which is the safe direction.
    if not has_scenarios and WEB_TASK_PATH_RE.search(tasks_text):
        findings.append("tasks touch browser surfaces (web file paths) but "
                        "specs/NNN/scenarios.md is missing — decompose must "
                        "emit BDD scenarios + a [qa:browser] gate")
    return findings


# ── Stream H: autonomy grant ledger + preflight (v3.18.0) ────────────────────
# Front-load run-time decisions to plan-time so unattended runs never stall:
# the operator approves a TYPED action list once (grant), the loop checks the
# ledger mechanically (check-grant), unlisted gates are recorded for morning
# resume (pending), and env/service requirements are proven reachable BEFORE
# the run starts (preflight). Fail closed everywhere.

# Threat model (codex v3.18 round 1, CRITICAL — documented decision, same as
# the v3.14 no-HMAC call): this ledger is an ANTI-ACCIDENT mechanism and an
# intent record, not an anti-adversary boundary. check-grant does not CONFER
# capability — an agent with shell access can already `git push` directly;
# the gate lives in the agent's instructions. Cryptographic operator binding
# for a local single-user store is over-engineering; strictness here is
# validation (typed actions, bounded TTL, fail-closed expiry), not signatures.
ACTION_PAT = re.compile(r"^[a-z][a-z0-9_-]*:\S+$")
GRANT_DEFAULT_TTL_HOURS = 72.0  # multi-day runs; 24h expired mid-run
GRANT_MAX_TTL_HOURS = 168.0  # 7 days — non-finite/zero/negative/huger rejected

# Artifact identity (spec-295 EDGE-006): an immutable OCI digest reference
# (name[:tag]@sha256:<64hex> — tag optional, digest mandatory and
# authoritative) or a 40-hex commit sha. A mutable tag WITHOUT a digest
# (`img:latest`) is never valid. re.fullmatch (not .match + $) — with
# .match, a trailing "$" still matches just before a trailing newline,
# letting a crafted "artifact\n" slip through (adversary HIGH #4).
ARTIFACT_DIGEST_PAT = re.compile(
    r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[A-Za-z0-9_][A-Za-z0-9._-]*)?"
    r"@sha256:[0-9a-f]{64}$")
ARTIFACT_SHA_PAT = re.compile(r"^[0-9a-f]{40}$")


def _now() -> float:
    import time
    return time.time()


def grant_actions(store: Path, run_id: str, actions: list[str], *,
                  ttl_hours: float = GRANT_DEFAULT_TTL_HOURS,
                  granted_by: str = "operator",
                  reason: str | None = None) -> bool:
    """Record operator-approved actions for a run. Actions must be typed
    ('type:target', e.g. 'push:origin/main') — free prose never matches at
    run time, so it is rejected here rather than silently failing at 3am.
    `reason` is additive (spec-295 REQ-07): when truthy it is sanitized and
    stored on each grant entry; omitted, every entry stays byte-identical to
    the pre-reason shape (no `reason` key at all) — the hotfix:prod-* escape
    reads this field, ordinary grants never set it."""
    import math
    if not actions or any(not ACTION_PAT.match(a) for a in actions):
        return False
    if not math.isfinite(ttl_hours) or not 0 < ttl_hours <= GRANT_MAX_TTL_HOURS:
        return False  # inf/0/negative/absurd TTL = effectively non-expiring
    clean_reason = sanitize_reason(reason) if isinstance(reason, str) else ""
    granted_at = _now()
    with _StoreLock(store):
        data = _load_store(store)
        auto = data.setdefault("_autonomy", {}).setdefault(run_id, {})
        grants = auto.setdefault("grants", {})
        for a in actions:
            entry = {
                "granted_at": granted_at,
                "expires_at": granted_at + ttl_hours * 3600,
                "granted_by": granted_by,
            }
            if clean_reason.strip():
                entry["reason"] = clean_reason
            grants[a] = entry
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


def _valid_artifact(artifact) -> bool:
    """True iff artifact is a str fullmatch of an immutable digest reference
    or a bare 40-hex commit sha (EDGE-006). Comparison downstream is
    exact-string on the whole recorded artifact — this function only proves
    the SHAPE is immutable, not that any two artifacts are equal."""
    if not isinstance(artifact, str):
        return False
    return bool(ARTIFACT_DIGEST_PAT.fullmatch(artifact)
                or ARTIFACT_SHA_PAT.fullmatch(artifact))


def _evidence_resolves(data: dict, evidence_ids: list[str], artifact: str) -> bool:
    """True iff EVERY evidence_id names a top-level store key carrying a
    successful runner-executed gate bound to this exact immutable artifact.
    Caller-recorded `record-gate --exit 0` evidence is deliberately rejected:
    a shell-capable agent can fabricate it, just as strict verify_done does."""
    if not evidence_ids:
        return False
    for eid in evidence_ids:
        gate = data.get(eid, {}).get("gate", {})
        if (gate.get("exit_code") != 0
                or gate.get("executed_by") != "run_gate"
                or gate.get("artifact") != artifact):
            return False
    return True


def _promotions_ns(data: dict, run_id: str) -> list:
    """Shape guard for the `_promotions` store namespace (adversary HIGH #5,
    ports the `_findings_ns` idiom): `_promotions` must be a dict-of-lists or
    absent; a per-run entry must be a list or absent. Either violation raises
    SystemExit rather than silently clobbering or appending to the wrong
    shape — an ordinary task-id record is dict-shaped too, so a bare
    isinstance(dict) check on `_promotions` alone would not be enough to
    prevent misinterpreting an unrelated record."""
    promotions = data.setdefault("_promotions", {})
    if not isinstance(promotions, dict):
        raise SystemExit("record-promotion: store key '_promotions' is not a "
                          "dict (schema conflict — refusing to overwrite)")
    run_entry = promotions.setdefault(run_id, [])
    if not isinstance(run_entry, list):
        raise SystemExit(f"record-promotion: '_promotions[{run_id}]' is not "
                          "a list (schema conflict — refusing to overwrite)")
    return run_entry


def record_promotion(store: Path, run_id: str, *, from_env: str, to_env: str,
                     surface: str, artifact, evidence_ids,
                     ttl_hours: float = GRANT_DEFAULT_TTL_HOURS) -> bool:
    """Record validated proof that `artifact` passed `from_env` staging,
    ONLY if the artifact identity is immutable and every evidence_id
    resolves to a real recorded successful gate. Returns False (no write)
    on ANY validation failure — mirrors grant_actions' fail-closed posture."""
    import math
    if not _valid_artifact(artifact):
        return False
    if isinstance(evidence_ids, str):
        return False
    try:
        evidence_ids = list(evidence_ids)
    except TypeError:
        return False
    if not evidence_ids or any(not isinstance(e, str) or not e for e in evidence_ids):
        return False
    if (not isinstance(ttl_hours, (int, float))
            or not math.isfinite(ttl_hours)
            or not 0 < ttl_hours <= GRANT_MAX_TTL_HOURS):
        return False
    recorded_at = _now()
    with _StoreLock(store):
        data = _load_store(store)
        if (not all(isinstance(value, str) and value.strip()
                    for value in (from_env, to_env, surface))
                or not _evidence_resolves(data, evidence_ids, artifact)):
            return False
        _promotions_ns(data, run_id).append({
            "from_env": from_env,
            "to_env": to_env,
            "surface": surface,
            "artifact": artifact,
            "evidence_ids": evidence_ids,
            "recorded_at": recorded_at,
            "expires_at": recorded_at + ttl_hours * 3600,
        })
        _save_store(store, data)
    return True


def _valid_promotion_record(data: dict, rec) -> bool:
    """Revalidate a persisted promotion before it can authorize a read.

    The ledger is an anti-accident authority, so a partially written or
    manually corrupted record must never become more permissive than the
    record_promotion write path. Require the immutable artifact, runner-bound
    evidence, and bounded timestamp envelope again on every read.
    """
    import math
    if not isinstance(rec, dict):
        return False
    artifact = rec.get("artifact")
    evidence_ids = rec.get("evidence_ids")
    recorded_at = rec.get("recorded_at")
    expires_at = rec.get("expires_at")
    if (not _valid_artifact(artifact)
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or any(not isinstance(eid, str) or not eid for eid in evidence_ids)
            or isinstance(recorded_at, bool)
            or not isinstance(recorded_at, (int, float))
            or not math.isfinite(recorded_at)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(expires_at)
            or expires_at <= recorded_at
            or expires_at - recorded_at > GRANT_MAX_TTL_HOURS * 3600):
        return False
    return _evidence_resolves(data, evidence_ids, artifact)


def check_promotion(store: Path, run_id: str, to_env: str, surface: str,
                    artifact, now: float | None = None) -> bool:
    """Fail-closed read (mirrors check_grant): True ONLY for an exact,
    unexpired, from_env=='staging' (when to_env=='prod'), to_env, surface,
    and artifact match. A missing OR malformed `_promotions` namespace
    (non-dict top level, non-list run entry, non-dict record, missing/
    non-numeric/non-finite expiry) returns False without raising, crashing,
    or writing — pure read, same posture as check_grant/check_preflight."""
    if not _valid_artifact(artifact):
        return False
    data = _load_store(store)
    promotions = data.get("_promotions")
    if not isinstance(promotions, dict):
        return False
    records = promotions.get(run_id)
    if not isinstance(records, list):
        return False
    effective_now = now if now is not None else _now()
    for rec in records:
        if not _valid_promotion_record(data, rec):
            continue
        if rec.get("to_env") != to_env:
            continue
        if to_env == "prod" and rec.get("from_env") != "staging":
            continue
        if rec.get("surface") != surface:
            continue
        if rec.get("artifact") != artifact:
            continue
        if effective_now < rec["expires_at"]:
            return True
    return False


# ── Stream H.2: prod-action precondition (spec-295 GAP-1) ───────────────────
# Wires check_promotion (Plan 01) into check-grant: a deploy:prod-* /
# flip:prod-* / migrate:prod-* action ADDITIONALLY requires a fresh
# staging->prod promote record for the EXACT artifact before it is
# authorized. Every other action type is untouched — check_grant_prod
# delegates straight to check_grant with zero side effects (REQ-05
# byte-identical guard).

# MAINTENANCE OBLIGATION (spec.md § Risks #2, A-002): this is a closed
# prefix list — it cannot code-enforce its own completeness. Any NEW
# prod-mutating action TYPE must be added here or it silently bypasses the
# precondition (fail-open by omission). Accepted, documented design risk —
# see the threat register (T-01-03), not a bug to "fix" here.
PROD_ACTION_PREFIXES = ("deploy:prod-", "flip:prod-", "migrate:prod-")
PROD_ACTION_TYPES = ("deploy", "flip", "migrate")


def _hotfix_prod_surface(action: str) -> str | None:
    """Return a production hotfix surface, including guarded variants.

    Any spelling that clearly targets prod must route through the reasoned,
    audited hotfix path instead of falling through ordinary check_grant.
    Empty strings identify production-looking actions with no surface so the
    caller can refuse them rather than treating them as non-production.
    """
    folded = action.casefold()
    for prefix in ("hotfix:prod-", "hotfix:prod_",
                   "hotfix:production-", "hotfix:production_"):
        if folded.startswith(prefix):
            return action[len(prefix):]
    if folded in ("hotfix:prod", "hotfix:production"):
        return ""
    return None


def _prod_surface(action: str) -> str | None:
    """Single source of truth for prod-surface extraction — every prod verb
    (deploy/flip/migrate) routes through this one function (RESEARCH Pitfall
    3). Returns the surface substring after the matched prefix, or None when
    `action` is not a prod-mutating action at all (non-prod fast path)."""
    folded_action = action.casefold()
    for prefix in PROD_ACTION_PREFIXES:
        if folded_action.startswith(prefix):
            return action[len(prefix):]
    # Fail closed on common spelling variants of the same production target.
    # These aliases are not the canonical vocabulary, but routing them through
    # the promotion gate prevents a granted `deploy:prod` / `flip:prod_api` /
    # `migrate:production-db` action from silently falling through to the
    # ordinary non-production check_grant path.
    for action_type in PROD_ACTION_TYPES:
        marker = f"{action_type}:"
        if not action.startswith(marker):
            continue
        target = action[len(marker):]
        folded_target = target.casefold()
        if folded_target == "prod":
            return ""  # invalid empty surface; promotion recording rejects it
        if folded_target.startswith("prod_"):
            return target[len("prod_"):]
        if folded_target == "production":
            return ""
        if folded_target.startswith(("production-", "production_")):
            return target[len("production-"):].lstrip("_")
    return None


def _surface_has_staging(surface: str, manifest: dict | None) -> bool:
    """True unless `manifest` explicitly declares `surface` with staging ==
    'none' (EDGE-003). manifest=None skips this check entirely — real
    config/parity-manifest.yaml wiring lands Phase 4, so the absence of a
    manifest here must never fabricate a pass OR a refusal."""
    if manifest is None:
        return True
    entry = manifest.get(surface)
    if not isinstance(entry, dict):
        return True
    return entry.get("staging") not in (None, "none")


def _promote_miss_reason(store: Path, run_id: str, surface: str, artifact,
                         now: float | None = None) -> str:
    """Classify why check_promotion returned False for this surface+artifact,
    distinguishing the three read-path typed reasons. Only staging->prod
    records for this surface count as 'a promote exists' — a dev->prod or
    prod->prod record is invisible here too (adversary CRITICAL #2, mirrors
    check_promotion's own from_env guard), so it always falls through to
    NO-PROMOTE-EVIDENCE rather than being misread as a mismatch/expiry."""
    data = _load_store(store)
    promotions = data.get("_promotions")
    if not isinstance(promotions, dict):
        return "NO-PROMOTE-EVIDENCE"
    records = promotions.get(run_id)
    if not isinstance(records, list):
        return "NO-PROMOTE-EVIDENCE"
    effective_now = now if now is not None else _now()
    surface_matches = False
    artifact_match_expired = False
    for rec in records:
        if not _valid_promotion_record(data, rec):
            continue
        if rec.get("to_env") != "prod" or rec.get("from_env") != "staging":
            continue
        if rec.get("surface") != surface:
            continue
        surface_matches = True
        if rec.get("artifact") != artifact:
            continue
        if effective_now >= rec["expires_at"]:
            artifact_match_expired = True
    if artifact_match_expired:
        return "PROMOTE-EXPIRED"
    if surface_matches:
        return "PROMOTE-ARTIFACT-MISMATCH"
    return "NO-PROMOTE-EVIDENCE"


def _check_hotfix_bypass(store: Path, run_id: str, action: str,
                         *, now: float | None = None) -> bool:
    """The ONE sanctioned promote-precondition escape (REQ-07/EDGE-004): a
    hotfix:prod-* action authorizes ONLY on an operator grant carrying a
    non-empty reason — deliberately does NOT call check_promotion, since
    bypassing the promote requirement is the entire point of the escape.
    No autonomous code path may call grant_actions on a hotfix:prod- action
    (process control, V4 access control) — the only way a hotfix grant
    exists is an explicit operator `grant ... --reason`."""
    if not check_grant(store, run_id, action, now=now):
        record_pending(store, run_id, action,
                       "NO-HOTFIX-GRANT: hotfix:prod-* requires an operator "
                       "grant carrying a non-empty --reason")
        return False
    entry = (_load_store(store).get("_autonomy", {})
             .get(run_id, {}).get("grants", {}).get(action)) or {}
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        record_pending(store, run_id, action,
                       "NO-HOTFIX-GRANT: hotfix:prod-* requires an operator "
                       "grant carrying a non-empty --reason")
        return False
    record_hotfix_bypass(store, run_id, action, reason)
    return True


def record_hotfix_bypass(store: Path, run_id: str, action: str, reason: str) -> bool:
    """Durable audit record for a hotfix:prod-* bypass (REQ-07/EDGE-004) —
    mirrors record_pending's lock/save shape. Append-only: each bypass
    (even a repeat during the same incident) gets its own entry, unlike
    record_pending's dedup — every use of the escape is individually
    auditable."""
    with _StoreLock(store):
        data = _load_store(store)
        auto = data.setdefault("_autonomy", {}).setdefault(run_id, {})
        auto.setdefault("hotfix_bypasses", []).append({
            "action": action,
            "reason": sanitize_reason(reason),
            "recorded_at": _now(),
        })
        _save_store(store, data)
    return True


def check_grant_prod(store: Path, run_id: str, action: str, artifact,
                     *, manifest: dict | None = None,
                     now: float | None = None) -> bool:
    """Fail-closed prod-action precondition: a deploy:prod-* / flip:prod-* /
    migrate:prod-* action additionally requires a fresh staging->prod promote
    record proving `artifact` passed staging on this surface, on top of the
    ordinary grant. Every non-prod action returns check_grant(...) UNCHANGED
    with zero side effects (REQ-05). record_pending fires ONLY on this prod
    path — check_grant itself stays pure (RESEARCH Pitfall 1).

    hotfix:prod-* is the ONE sanctioned bypass of this entire precondition
    (REQ-07/EDGE-004) — routed to _check_hotfix_bypass BEFORE the ordinary
    prod-prefix dispatch, since 'hotfix:prod-' is not in PROD_ACTION_PREFIXES
    and never requires promote evidence."""
    hotfix_surface = _hotfix_prod_surface(action)
    if hotfix_surface is not None:
        if not hotfix_surface:
            record_pending(store, run_id, action,
                           "NO-HOTFIX-GRANT: production hotfix requires a "
                           "non-empty target surface")
            return False
        return _check_hotfix_bypass(store, run_id, action, now=now)

    surface = _prod_surface(action)
    if surface is None:
        return check_grant(store, run_id, action, now=now)

    if not artifact:
        record_pending(store, run_id, action,
                       "NO-PROMOTE-EVIDENCE: --artifact is required for a "
                       "prod-targeting action")
        return False

    if manifest is not None and not _surface_has_staging(surface, manifest):
        record_pending(store, run_id, action,
                       f"NO-STAGING-COUNTERPART: surface '{surface}' has no "
                       "staging counterpart per the parity manifest")
        return False

    if not check_grant(store, run_id, action, now=now):
        record_pending(store, run_id, action,
                       "needs operator grant for this prod action (a promote "
                       "record confers no authority on its own)")
        return False

    if check_promotion(store, run_id, "prod", surface, artifact, now=now):
        return True

    reason = _promote_miss_reason(store, run_id, surface, artifact, now=now)
    record_pending(store, run_id, action,
                   f"{reason}: no fresh staging->prod promote record matches "
                   f"artifact {artifact} for surface '{surface}'")
    return False


def record_pending(store: Path, run_id: str, action: str, reason: str) -> bool:
    """An unlisted gate hit mid-run: STOP, but leave a durable record so the
    morning resume is one `grant` command (long-run-continuity port)."""
    if not ACTION_PAT.match(action):
        return False
    with _StoreLock(store):
        data = _load_store(store)
        auto = data.setdefault("_autonomy", {}).setdefault(run_id, {})
        pending = auto.setdefault("pending", [])
        if not any(p["action"] == action for p in pending):
            pending.append({"action": action,
                            "reason": sanitize_reason(reason),
                            "recorded_at": _now()})
        _save_store(store, data)
    return True


def list_pending(store: Path, run_id: str) -> list[dict]:
    return list(_load_store(store).get("_autonomy", {})
                .get(run_id, {}).get("pending", []))


def preflight_check(requirements: list[dict], timeout: int = 30, *,
                    store: Path | None = None, run_id: str | None = None) -> dict:
    """Prove env vars present and services reachable BEFORE an unattended run.
    Env checks report presence only — a secret value never enters the result.
    Probes execute for real (exit 0 = reachable), but never through an
    implicit shell. Empty manifest fails: an unattended run with nothing
    declared is undeclared, not requirement-free."""
    results: list[dict] = []
    for req in requirements:
        kind, name = req.get("kind"), req.get("name", "")
        if kind == "env":
            ok = bool(os.environ.get(name))
            detail = "present" if ok else "MISSING"
        elif kind == "probe":
            argv = req.get("argv")
            if not (isinstance(argv, list) and argv
                    and all(isinstance(arg, str) and arg and "\0" not in arg
                            for arg in argv)):
                ok = False
                detail = "INVALID: probe requires a non-empty argv string array"
            elif any(re.search(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})", arg)
                     for arg in argv):
                ok = False
                detail = "INVALID: environment placeholders are not allowed in probe argv"
            else:
                # Probe processes inherit the environment. Secrets must stay
                # there instead of becoming OS-visible process arguments.
                command = list(argv)
                try:
                    proc = subprocess.run(command, shell=False,
                                          capture_output=True, timeout=timeout)
                    ok = proc.returncode == 0
                    detail = f"exit {proc.returncode}"
                except subprocess.TimeoutExpired:
                    ok, detail = False, f"timeout after {timeout}s"
                except OSError:
                    ok, detail = False, "executable unavailable"
        elif kind == "staging-proof":
            artifact = req.get("artifact")
            if store is None or run_id is None:
                ok, detail = False, "NO-STORE-AVAILABLE: no store/run_id to check the promote ledger"
            else:
                ok = check_promotion(store, run_id, "prod", name, artifact)
                detail = "promoted" if ok else "NO-PROMOTE-EVIDENCE: no fresh staging->prod promote record"
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
    # future-dated checked_at = corrupt/forged record, not "fresh" — fail closed
    return 0 <= age < max_age_hours * 3600


# ── Stream I: findings queue (REQ-06 / AC-006) ───────────────────────────────
# Persistent review-findings queue: work ALL findings, dedup re-runs. Reuses
# the existing store/lock/atomic-write machinery — one more top-level
# namespace on evidence.json, like `_autonomy` (RESEARCH Pattern 3, plan.md
# single-authority constraint). Additive only — never touches verify_done /
# run_gate.

def _normalize(s: str) -> str:
    """Whitespace-collapse + lowercase ONLY — no stemming/synonym folding
    (EDGE-004: dedup is exact-normalized, not fuzzy — documented limitation)."""
    return " ".join(s.split()).lower()


def _findings_ns(data: dict) -> list:
    """Shape guard: the `findings` key must be a list or absent. A prior
    task entry named `findings` (dict-shaped, like every other top-level
    task-id record) must error, never be silently clobbered (adversary F7)."""
    findings = data.setdefault("findings", [])
    if not isinstance(findings, list):
        raise SystemExit("findings-queue: store key 'findings' is not a list "
                          "(schema conflict — refusing to overwrite)")
    return findings


def findings_add(store: Path, file: str, issue: str) -> tuple[str, bool]:
    """Queue a review finding. Returns (sig, deduped) computed INSIDE the
    store lock — the dedup outcome is atomic with the write, never a racy
    pre-read (adversary F2). `file` is a free-text display/hash field only —
    never opened or path-resolved (no traversal surface)."""
    sig = hashlib.sha256(json.dumps([file, _normalize(issue)]).encode()).hexdigest()
    with _StoreLock(store):
        data = _load_store(store)
        findings = _findings_ns(data)
        deduped = any(f["sig"] == sig for f in findings)
        if not deduped:
            findings.append({"sig": sig, "file": file, "issue": issue,
                             "resolved": False, "recorded_at": _now()})
            _save_store(store, data)
    return sig, deduped


def findings_list(store: Path, unresolved: bool = False) -> list:
    findings = _findings_ns(_load_store(store))
    if unresolved:
        return [f for f in findings if not f.get("resolved")]
    return list(findings)


def findings_resolve(store: Path, sig: str) -> bool:
    """Marks the matching signature resolved. False when sig unknown — no
    write happens on a miss (adversary F4)."""
    with _StoreLock(store):
        data = _load_store(store)
        findings = _findings_ns(data)
        for f in findings:
            if f["sig"] == sig:
                f["resolved"] = True
                _save_store(store, data)
                return True
    return False


# ── CLI ──────────────────────────────────────────────────────────────────────

def _store_path() -> Path:
    return Path(os.environ.get("GATES_STORE", ".feature-fix-swarm/evidence.json"))


# ── delegation-audit: orchestrator discipline (advisory, never blocks) ───────
# Build-type spawn descriptions — an Agent/Task spawn matching this with no
# explicit model pin inherits the orchestrator tier (premium-cost bug).
BUILD_DESC_PAT = re.compile(r"\b(rebase|adopt|prep|fix|implement|merge)\b", re.I)
# Main-loop Bash commands the orchestrator must delegate, not run inline.
TRIPWIRE_REBASE_PAT = re.compile(r"\bgit rebase\b|git checkout --(theirs|ours)")
TRIPWIRE_LOOP_PAT = re.compile(r"\b(for|while)\b.*\bdo\b", re.S)
TRIPWIRE_LOOP_BODY_PAT = re.compile(r"sed -i|git show[^\n]*>", re.S)
# Legitimate inline loops: CI-watch / poll monitors are the orchestrator's job.
POLL_PAT = re.compile(r"seen\.txt|gh (run|pr) (watch|checks)|--watch\b")


def _is_tripwire(cmd: str) -> bool:
    if TRIPWIRE_REBASE_PAT.search(cmd):
        return True
    return bool(TRIPWIRE_LOOP_PAT.search(cmd) and TRIPWIRE_LOOP_BODY_PAT.search(cmd))


def delegation_audit(transcript_text: str) -> dict:
    """Scan a Claude Code session transcript (JSONL) for delegation drift.

    Counts main-loop Agent/Task spawns by model pin and main-loop Bash
    trip-wires. Sidechain entries (agent sub-transcripts) are ignored —
    a sub-agent running `git rebase` is delegation working as intended.
    """
    hist: dict[str, int] = {}
    unpinned_build: list[str] = []
    inline_mechanical: list[str] = []
    advisor_calls = 0
    for line in transcript_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("isSidechain"):
            continue
        msg = entry.get("message") or {}
        if entry.get("type") != "assistant" or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, inp = block.get("name", ""), block.get("input") or {}
            if name in ("Agent", "Task"):
                model = (inp.get("model") or "").strip().lower() or "inherit"
                hist[model] = hist.get(model, 0) + 1
                # Advisor-call budget (borrowed: advisor-call-budget): a
                # premium-tier pin is an advisor consult. Counted so a cap can
                # make the cheap-executor discount enforceable, not assumed.
                if "opus" in model or "fable" in model:
                    advisor_calls += 1
                desc = inp.get("description") or ""
                if model == "inherit" and BUILD_DESC_PAT.search(desc):
                    unpinned_build.append(desc[:80])
            elif name == "Bash":
                cmd = inp.get("command") or ""
                if POLL_PAT.search(cmd):
                    continue
                if _is_tripwire(cmd):
                    inline_mechanical.append((inp.get("description") or cmd)[:80])
    return {"histogram": hist, "unpinned_build": unpinned_build,
            "inline_mechanical": inline_mechanical,
            "advisor_calls": advisor_calls}


def _manifest_scalar(raw: str, *, line_no: int) -> str:
    """Parse the scalar subset used by the parity-manifest contract."""
    value = raw.strip()
    if not value:
        raise ValueError(f"empty manifest scalar at line {line_no}")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid quoted manifest scalar at line {line_no}") from exc
        if not isinstance(parsed, str):
            raise ValueError(f"manifest scalar must be text at line {line_no}")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(f"invalid quoted manifest scalar at line {line_no}")
        return value[1:-1].replace("''", "'")
    # Inline comments begin only after whitespace, matching the simple scalar
    # shape of the committed parity manifest without pretending to parse all YAML.
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    if not value:
        raise ValueError(f"empty manifest scalar at line {line_no}")
    return value


def _normalize_manifest(data: dict) -> dict:
    """Normalize legacy JSON maps and the committed `surfaces:` row shape."""
    if "surfaces" not in data:
        return data
    rows = data.get("surfaces")
    if not isinstance(rows, list) or not rows:
        raise ValueError("manifest surfaces must be a non-empty list")
    normalized: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each manifest surface must be an object")
        surface = row.get("surface")
        staging = row.get("staging_instance", row.get("staging"))
        if not isinstance(surface, str) or not surface.strip():
            raise ValueError("each manifest surface needs a non-empty surface name")
        if not isinstance(staging, str) or not staging.strip():
            raise ValueError(f"manifest surface {surface!r} needs staging_instance")
        if surface in normalized:
            raise ValueError(f"duplicate manifest surface: {surface}")
        normalized[surface] = {"staging": staging}
    return normalized


def _parse_parity_manifest_yaml(text: str) -> dict:
    """Parse only the dependency-free YAML subset used by parity-manifest.yaml.

    check-grant needs two fields: `surface` and `staging_instance`. Restricting
    the parser to those flat rows keeps gates.py zero-install while malformed,
    empty, and duplicate rows still fail closed.
    """
    in_surfaces = False
    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if not in_surfaces:
            if stripped == "surfaces:":
                in_surfaces = True
            continue
        if indent == 0:
            break
        surface_match = re.fullmatch(r"-\s+surface:\s*(.+)", stripped)
        if surface_match:
            if current is not None:
                rows.append(current)
            current = {
                "surface": _manifest_scalar(surface_match.group(1), line_no=line_no),
            }
            continue
        field_match = re.fullmatch(r"(staging_instance|staging):\s*(.+)", stripped)
        if current is not None and field_match:
            current["staging_instance"] = _manifest_scalar(
                field_match.group(2), line_no=line_no,
            )
    if current is not None:
        rows.append(current)
    if not in_surfaces:
        raise ValueError("YAML manifest must contain a top-level surfaces list")
    return _normalize_manifest({"surfaces": rows})


def _load_manifest(path: str) -> dict:
    """Load a JSON or constrained YAML staging-parity manifest.

    Legacy JSON maps remain accepted. The committed YAML row shape is
    normalized to the `{surface: {staging: value}}` form consumed by the prod
    grant precondition. Invalid input raises ValueError so the CLI fails closed.
    """
    text = Path(path).read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _parse_parity_manifest_yaml(text)
    if not isinstance(data, dict):
        raise ValueError("manifest must parse to an object")
    return _normalize_manifest(data)


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
        gate_args = args[1:sep]
        artifact = _flag(gate_args, "--artifact") or None
        if "--artifact" in gate_args and artifact is None:
            print("GATE-REJECTED: --artifact requires a value", file=sys.stderr)
            return 1
        try:
            rc = run_gate(
                store, args[0], args[sep + 1:] if "--" in args else args[1:],
                artifact=artifact,
            )
        except ValueError as exc:
            print(f"GATE-REJECTED: {exc}", file=sys.stderr)
            return 1
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
    if cmd == "delegation-audit":
        threshold = int(_flag(args, "--threshold", "3"))
        # Advisor-call cap (advisory): premium-tier spawns per transcript.
        # Unset = count-only, no cap check.
        advisor_cap_raw = _flag(args, "--advisor-cap", "")
        advisor_cap = int(advisor_cap_raw) if advisor_cap_raw != "" else None
        pos, skip = [], False
        for a in args:
            if skip:
                skip = False
                continue
            if a in ("--threshold", "--advisor-cap"):
                skip = True
                continue
            pos.append(a)
        text = Path(pos[0]).read_text() if pos else sys.stdin.read()
        res = delegation_audit(text)
        total = sum(res["histogram"].values())
        print(f"SPAWNS: {total}")
        for m, n in sorted(res["histogram"].items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {m}: {n}")
        print(f"ADVISOR-CALLS: {res['advisor_calls']}"
              + (f" (cap {advisor_cap})" if advisor_cap is not None else ""))
        if advisor_cap is not None and res["advisor_calls"] > advisor_cap:
            print(f"ADVISOR-WARN: {res['advisor_calls']} premium consult(s) "
                  f"exceed the cap of {advisor_cap} — an uncapped advisor "
                  "erodes the cheap-executor discount. Advisory only.")
        for d in res["unpinned_build"]:
            print(f"UNPINNED-BUILD: {sanitize_reason(d)}")
        for d in res["inline_mechanical"]:
            print(f"INLINE-MECHANICAL: {sanitize_reason(d)}")
        nb, nm = len(res["unpinned_build"]), len(res["inline_mechanical"])
        print(f"unpinned-build={nb} inline-mechanical={nm} threshold={threshold}")
        if nb > 0 or nm > threshold:
            print("DELEGATION-WARN: pin `model` on build spawns; delegate "
                  "mechanical loops (see skills/feature-spec/SKILL.md § "
                  "Delegation discipline). Advisory only — cost drift, not "
                  "correctness.")
        else:
            print("DELEGATION-OK")
        return 0  # advisory: never blocks
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
        reason = _flag(args, "--reason") or None
        if not grant_actions(store, run_id, actions, ttl_hours=ttl, reason=reason):
            print("GRANT-REJECTED: actions must be typed 'type:target' "
                  "(e.g. push:origin/main) and non-empty")
            return 1
        for a in actions:
            print(f"GRANTED: {a} (run {run_id}, ttl {ttl}h)")
        return 0
    if cmd == "promote":
        run_id = args[0]
        from_env, to_env = _flag(args, "--from"), _flag(args, "--to")
        surface, artifact = _flag(args, "--surface"), _flag(args, "--artifact")
        try:
            evidence_ids = [args[i + 1] for i, a in enumerate(args)
                            if a == "--evidence"]
        except IndexError:
            print("PROMOTE-REJECTED: --evidence requires a value", file=sys.stderr)
            return 1
        try:
            ttl = float(_flag(args, "--ttl-hours", str(GRANT_DEFAULT_TTL_HOURS)))
        except ValueError:
            print("PROMOTE-REJECTED: --ttl-hours must be numeric", file=sys.stderr)
            return 1
        if not record_promotion(store, run_id, from_env=from_env, to_env=to_env,
                                surface=surface, artifact=artifact,
                                evidence_ids=evidence_ids, ttl_hours=ttl):
            print(f"PROMOTE-REJECTED: {sanitize_reason(surface)}@"
                  f"{sanitize_reason(str(artifact))} failed validation "
                  "(malformed artifact identity, empty/unresolved evidence, "
                  "or out-of-bounds --ttl-hours)", file=sys.stderr)
            return 1
        print(f"PROMOTED: {sanitize_reason(surface)}@{sanitize_reason(str(artifact))} "
              f"({sanitize_reason(from_env)}->{sanitize_reason(to_env)}, run {run_id})")
        return 0
    if cmd == "check-grant":
        run_id, action = args[0], _flag(args, "--action")
        safe = sanitize_reason(action)
        hotfix_surface = _hotfix_prod_surface(action)
        if hotfix_surface is not None:
            if check_grant_prod(store, run_id, action, None):
                entry = (_load_store(store).get("_autonomy", {})
                        .get(run_id, {}).get("grants", {}).get(action)) or {}
                reason = sanitize_reason(entry.get("reason", ""))
                print(f"EMERGENCY BYPASS: {safe} authorized WITHOUT promote "
                      f"evidence (run {run_id}) — reason: {reason}")
                return 0
            print(f"NOT-GRANTED: {safe} (run {run_id}) — hotfix:prod-* is the "
                  "sanctioned emergency escape and requires an operator "
                  f"`gates.py grant {run_id} --action '{action}' --reason "
                  "\"...\"`, STOP, and wait for operator")
            return 1
        prod_surface = _prod_surface(action)
        if prod_surface is not None:
            artifact = _flag(args, "--artifact") or None
            manifest_path = _flag(args, "--manifest")
            manifest = None
            if manifest_path:
                try:
                    manifest = _load_manifest(manifest_path)
                except (OSError, ValueError) as exc:
                    print(f"CHECK-GRANT-REJECTED: cannot load manifest "
                          f"{manifest_path}: {exc}", file=sys.stderr)
                    return 1
            if check_grant_prod(store, run_id, action, artifact, manifest=manifest):
                print(f"GRANTED: {safe}")
                return 0
            print(f"NOT-GRANTED: {safe} (run {run_id}) — record promote "
                  f"evidence via `gates.py promote {run_id} --from staging "
                  f"--to prod --surface {prod_surface} --artifact <digest> "
                  f"--evidence <id>`, then `gates.py grant {run_id} --action "
                  f"'{action}'`, STOP, and wait for operator")
            return 1
        if check_grant(store, run_id, action):
            print(f"GRANTED: {safe}")
            return 0
        print(f"NOT-GRANTED: {safe} (run {run_id}) — record with "
              f"`gates.py pending {run_id} --action '{action}' --reason …`, "
              f"STOP, and wait for operator grant")
        return 1
    if cmd == "pending":
        run_id = args[0]
        action = _flag(args, "--action")
        if action:
            if not record_pending(store, run_id, action, _flag(args, "--reason")):
                print("PENDING-REJECTED: action must be typed 'type:target'")
                return 1
            print(f"PENDING-RECORDED: {action} (run {run_id})")
            return 0
        pend = list_pending(store, run_id)
        for p in pend:
            print(f"PENDING: {sanitize_reason(p['action'])} — "
                  f"{sanitize_reason(p['reason'])}")
        print("NO-PENDING" if not pend else
              f"PENDING-COUNT: {len(pend)} — approve via `gates.py grant "
              f"{run_id} --action <action>` then resume")
        return 1 if pend else 0
    if cmd == "preflight":
        manifest = Path(args[0])
        run_id = _flag(args, "--run", "default")
        with open(manifest) as f:
            requirements = json.load(f)
        result = preflight_check(requirements, store=store, run_id=run_id)
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
    if cmd == "findings-queue":
        sub = args[0] if args else ""
        rest = args[1:]
        try:
            if sub == "add":
                if len(rest) < 2:
                    print("usage: findings-queue add <file> <issue>", file=sys.stderr)
                    return 2
                sig, deduped = findings_add(store, rest[0], rest[1])
                print(json.dumps({"sig": sig, "deduped": deduped}))
                return 0
            if sub == "list":
                unresolved = "--unresolved" in rest
                print(json.dumps(findings_list(store, unresolved=unresolved)))
                return 0
            if sub == "resolve":
                if not rest:
                    print("usage: findings-queue resolve <sig>", file=sys.stderr)
                    return 2
                sig = rest[0]
                if findings_resolve(store, sig):
                    print(json.dumps({"sig": sig, "resolved": True}))
                    return 0
                print(f"unknown signature: {sig}", file=sys.stderr)
                return 1
            print("usage: findings-queue add|list|resolve ...", file=sys.stderr)
            return 2
        except SystemExit as e:
            print(str(e), file=sys.stderr)
            return 3
    if cmd == "analyze":
        with open(args[0]) as f:
            spec = f.read()
        with open(args[1]) as f:
            tasks = f.read()
        # scenarios.md sibling of tasks.md marks the spec browser-touchable
        has_scenarios = (
            Path(args[1]).resolve().parent / "scenarios.md").is_file()
        findings = analyze_artifacts(spec, tasks, has_scenarios=has_scenarios)
        for f in findings:
            print(f"ANALYZE: {f}")
        print("ANALYZE-PASS" if not findings else f"ANALYZE-FAIL: {len(findings)} findings")
        return 1 if findings else 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
