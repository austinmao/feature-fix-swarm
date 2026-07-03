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
    python3 lib/gates.py record-red   T041 --exit 1 < red-run.log
    python3 lib/gates.py check-red    T041        # exit 0 iff RED proven
    python3 lib/gates.py scan-tamper  < diff.txt  # exit 1 + findings if hacked
    python3 lib/gates.py analyze SPEC_FILE TASKS_FILE

Evidence store path: $GATES_STORE (default .feature-fix-swarm/evidence.json).
"""
from __future__ import annotations

import fcntl
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
        }
        _save_store(store, data)


def run_gate(store: Path, task_id: str, cmd: list[str], timeout: int = 1800) -> int:
    """Execute the gate command and record the REAL exit code (P1: evidence
    bound to the runner, not caller-supplied --exit). Returns the exit code."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    tail = (proc.stdout + proc.stderr)[-2000:]
    with _StoreLock(store):
        data = _load_store(store)
        entry = data.setdefault(task_id, {})
        entry["gate"] = {
            "exit_code": proc.returncode,
            "cmd": " ".join(cmd),
            "tests_before": "",
            "tests_after": tail.splitlines()[-1] if tail.splitlines() else "",
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


def verify_done(store: Path, task_id: str) -> bool:
    """A task is done ONLY if recorded gate evidence exists with exit 0."""
    gate = _load_store(store).get(task_id, {}).get("gate")
    return bool(gate) and gate.get("exit_code") == 0


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
            if in_test_file and re.search(r"\bassert\b|expect\(", line):
                findings.append(f"assert deleted from test file: {line.strip()}")
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
        if re.search(r"US\d+", phase) and not any(
                re.search(r"\[qa:[a-z0-9,-]*e2e", ln) for ln in lines):
            findings.append(f"{phase}: story phase has no e2e smoke task")
    return findings


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
        record_gate_evidence(store, args[0], exit_code=int(_flag(args, "--exit", "1")),
                             cmd=_flag(args, "--cmd"), tests_before=_flag(args, "--before"),
                             tests_after=_flag(args, "--after"))
        return 0
    if cmd == "verify-done":
        ok = verify_done(store, args[0])
        print("DONE-VERIFIED" if ok else f"NOT-DONE: no passing gate evidence for {args[0]}")
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
