#!/usr/bin/env bash
# gsd-phase-evidence-gate.sh — PreToolUse (Edit|Write) BLOCK (exit 2)
#
# gsd analog of openclaw's checkbox-evidence-gate.sh, re-keyed to gsd-core
# planning artifacts (Phase B of spec 002; capability-native verify:post gates
# are non-functional at gsd-core 1.6.1 — upstream #2004/#2009 — so completion
# authority is enforced host-side). A phase-complete flip in
# .planning/ROADMAP.md (`- [ ] **Phase N:` → `- [x]`) or a completed_phases
# increment in .planning/STATE.md frontmatter is only legal when gates.py
# verify-done has runner-executed evidence for that phase — keyed by the
# phase-dir basename under .planning/phases/ (what gates-test-command.sh
# actually records via GSD_PHASE_ID) or the legacy `gsd-phase-N` id.
# The generic `gsd-phase` fallback was removed after P4-W6: a stale
# generic-key record from an unrelated run let an executor flip a phase
# complete with zero evidence for THAT phase.
#
# Bypass: GATES_BYPASS=1 (manual operator corrections only).
# Fail-open WARN when gates.py is not installed.
set -euo pipefail

[ "${GATES_BYPASS:-0}" = "1" ] && exit 0

INPUT=$(cat)

python3 - "$INPUT" <<'PY'
import json, os, re, subprocess, sys
from pathlib import Path

try:
    data = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)

ti = data.get("tool_input", {}) or {}
path = ti.get("file_path", "") or ""

is_roadmap = bool(re.search(r"\.planning/ROADMAP\.md$", path))
is_state = bool(re.search(r"\.planning/STATE\.md$", path))
if not (is_roadmap or is_state):
    sys.exit(0)

if data.get("tool_name") == "Edit":
    before, after = ti.get("old_string", ""), ti.get("new_string", "")
else:  # Write — diff against disk
    after = ti.get("content", "")
    try:
        before = Path(path).read_text()
    except Exception:
        sys.exit(0)

needed = []  # phase numbers requiring evidence

if is_roadmap:
    PHASE = re.compile(r"^\s*- \[([ xX])\] \**Phase (\d+)", re.M)
    def done(text):
        return {m.group(2).lstrip("0") or "0" for m in PHASE.finditer(text or "")
                if m.group(1).strip()}
    needed = sorted(done(after) - done(before))

if is_state:
    CP = re.compile(r"^\s*completed_phases:\s*(\d+)", re.M)
    b = CP.search(before or ""); a = CP.search(after or "")
    if b and a and int(a.group(1)) > int(b.group(1)):
        needed = [a.group(1)]

if not needed:
    sys.exit(0)

top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                     capture_output=True, text=True).stdout.strip()
cands = [
    os.path.join(top, "packages/feature-fix-swarm/lib/gates.py") if top else "",
    os.path.expanduser("~/.claude/lib/feature-fix-swarm/gates.py"),
    os.path.join(top, "lib/gates.py") if top else "",
]
gates = next((c for c in cands if c and os.path.isfile(c)), None)
if not gates:
    print("[gsd-phase-evidence-gate] WARN: gates.py not installed — flip allowed unverified",
          file=sys.stderr)
    sys.exit(0)

env = dict(os.environ, GATES_STRICT="1")

def verified(tid):
    return subprocess.run(["python3", gates, "verify-done", tid],
                          capture_output=True, text=True, env=env).returncode == 0

# Evidence keys, in the order they exist in the wild:
#   1. phase-dir basename (e.g. "03-pretooluse-guard") — what
#      gates-test-command.sh records when GSD_PHASE_ID is set
#   2. legacy gsd-phase-N
# NO generic `gsd-phase` fallback (P4-W6): cross-phase evidence must never
# unlock a flip.
def phase_dir_ids(n):
    pdir = Path(path).parent / "phases"
    ids = []
    try:
        for d in pdir.iterdir():
            m = re.match(r"^0*(\d+)-", d.name)
            if d.is_dir() and m and (m.group(1).lstrip("0") or "0") == n:
                ids.append(d.name)
    except OSError:
        pass
    return ids

blocked = [n for n in needed
           if not (any(verified(i) for i in phase_dir_ids(n))
                   or verified(f"gsd-phase-{n}"))]

if blocked:
    print("[gsd-phase-evidence-gate] BLOCKED — phase-complete flip without passing gate evidence:")
    for n in blocked:
        dirs = ", ".join(phase_dir_ids(n)) or f"gsd-phase-{n}"
        print(f"  Phase {n}: no verify-done evidence for {dirs}")
    print("Run the gate first:  GSD_PHASE_ID=<phase-dir-basename> bash scripts/gsd/gates-test-command.sh")
    print("Operator bypass (manual corrections only): GATES_BYPASS=1")
    sys.exit(2)
sys.exit(0)
PY
