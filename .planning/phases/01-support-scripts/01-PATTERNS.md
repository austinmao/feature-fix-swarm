# Phase 1: Support scripts - Pattern Map

**Mapped:** 2026-07-05
**Files analyzed:** 4 (2 scripts + 2 bats test files)
**Analogs found:** 4 / 4

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|--------------------|------|-----------|-----------------|----------------|
| `scripts/gsd/consent-check.sh` | CLI utility (fail-closed subprocess wrapper) | request-response (invoke node CLI, parse JSON, exit code) | `scripts/gsd/gates-test-command.sh` | exact (same role: thin bash wrapper delegating to another tool, fail-closed exit-code contract) |
| `scripts/gsd/state-phase.sh` | CLI utility (file parse/transform) | transform (read file, extract value, print) | `scripts/gsd/gsd-run.sh` (structure/usage idiom) + Pattern 3 in RESEARCH.md (content logic, no existing analog for text-extraction body) | role-match for shell conventions; no existing script does BODY-text extraction — RESEARCH.md Pattern 3 is the primary source |
| `tests/consent-check.bats` | test | request-response (stub external binary, assert exit code) | `scripts/harness/ruflo-host-executor.bats` | exact (same role: bats test stubbing a binary on PATH, using `$BATS_TEST_TMPDIR`, `run` + exit-status/output assertions) |
| `tests/state-phase.bats` | test | transform (fixture file in, stdout value out) | `scripts/harness/ruflo-host-executor.bats` (structural conventions only — no stubbing needed) | role-match (bats structure); content is fixture-based, simpler than the stubbing pattern |

## Pattern Assignments

### `scripts/gsd/consent-check.sh` (CLI utility, request-response / fail-closed wrapper)

**Analog:** `scripts/gsd/gates-test-command.sh` (structure) + RESEARCH.md Pattern 1/2 (content — already concrete, repo-verified)

**Shebang + strict mode + REPO_ROOT pattern** (`gates-test-command.sh` lines 1-6):
```bash
#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```

**Usage/exit-2 idiom** (`scripts/gsd/gsd-run.sh` lines 5-10):
```bash
set -uo pipefail

if [ $# -lt 1 ]; then
  echo "usage: gsd-run.sh <slash-command> [args...]" >&2
  exit 2
fi
```
Apply the same shape: `if [ $# -ne 1 ]; then echo "usage: consent-check.sh <capability-id>" >&2; exit 2; fi`.

**Fail-closed dependency check** (`gates-test-command.sh` lines 9-23, adapted for `node`/`gsd-tools` per RESEARCH.md Pattern 1):
```bash
if ! command -v node >/dev/null 2>&1; then
  echo "consent-check: node not found on PATH — cannot verify capability, failing closed" >&2
  exit 1
fi

GSD_TOOLS="$REPO_ROOT/node_modules/.bin/gsd-tools"
if [ ! -e "$GSD_TOOLS" ]; then
  echo "consent-check: $GSD_TOOLS not found — cannot verify capability, failing closed" >&2
  exit 1
fi
```

**Core pattern — invoke + argv-safe JSON parse** (RESEARCH.md Pattern 2, verified against live `gsd-tools capability list` output this session):
```bash
CAP_ID="$1"
LIST_JSON="$(node "$GSD_TOOLS" capability list 2>/dev/null)"
LIST_RC=$?
if [ "$LIST_RC" -ne 0 ] || [ -z "$LIST_JSON" ]; then
  echo "consent-check: gsd-tools capability list failed (rc=$LIST_RC) — failing closed" >&2
  exit 1
fi

printf '%s' "$LIST_JSON" | node -e '
  let raw = "";
  process.stdin.on("data", c => raw += c);
  process.stdin.on("end", () => {
    const capId = process.argv[1];
    let list;
    try { list = JSON.parse(raw); } catch { process.exit(1); }
    const entry = Array.isArray(list) ? list.find(e => e && e.id === capId) : null;
    process.exit(entry && entry.status === "active" ? 0 : 1);
  });
' "$CAP_ID"
CHECK_RC=$?
if [ "$CHECK_RC" -ne 0 ]; then
  echo "consent-check: capability '$CAP_ID' is not active/consented on this machine — run 'gsd capability install $CAP_ID' (or the appropriate gsd onboarding step) first" >&2
  exit 1
fi
exit 0
```

**Error handling pattern:** every failure path (missing node, missing binary, non-zero exit, unparsable JSON, capability absent/inactive) resolves to `exit 1` (never `exit 0`) — mirrors `gates-test-command.sh`'s `run_rc` / `verify-done` propagation style (lines 35-41: check rc, exit early on failure, never silently continue).

**Injection avoidance (critical, security-tagged in RESEARCH.md):** never interpolate `$CAP_ID` into the `node -e` source string — pass via `process.argv[1]` as shown above. Same class of guard as `HOST_EXECUTOR_ROOT`-style env-var overrides in the harness bats file, but here applied to argv, not env.

---

### `scripts/gsd/state-phase.sh` (CLI utility, transform)

**Analog:** `scripts/gsd/gsd-run.sh` (shebang/strict-mode/usage conventions only) + RESEARCH.md Pattern 3 (full content — no existing repo script does text extraction, so this is the primary source)

**Shebang + strict mode:**
```bash
#!/usr/bin/env bash
set -uo pipefail
```

**Core pattern — default arg + missing-file exit 2 + frontmatter-stripping BODY extraction** (RESEARCH.md Pattern 3, full text):
```bash
STATE_FILE="${1:-.planning/STATE.md}"
if [ ! -f "$STATE_FILE" ]; then
  echo "state-phase: $STATE_FILE not found" >&2
  exit 2
fi

BODY="$(awk '
  BEGIN{fm=0}
  NR==1 && $0=="---"{fm=1; next}
  fm==1 && $0=="---"{fm=2; next}
  fm!=1{print}
' "$STATE_FILE")"

PHASE_LINE="$(printf '%s\n' "$BODY" | grep -E '^Phase:[[:space:]]*[0-9]+[[:space:]]+of[[:space:]]+[0-9]+' | head -1)"
if [ -z "$PHASE_LINE" ]; then
  echo "state-phase: no 'Phase: X of Y' line found in $STATE_FILE body" >&2
  exit 1
fi
CURRENT="$(printf '%s\n' "$PHASE_LINE" | sed -E 's/^Phase:[[:space:]]*([0-9]+).*/\1/')"

STATUS_LINE="$(printf '%s\n' "$BODY" | grep -E '^Status:' | head -1)"
if printf '%s' "$STATUS_LINE" | grep -qi 'phase complete'; then
  COMPLETED="$CURRENT"
else
  COMPLETED=$((CURRENT - 1))
  [ "$COMPLETED" -lt 0 ] && COMPLETED=0
fi

printf '%s\n' "$COMPLETED"
exit 0
```

**Off-by-one rule (must be locked by fixture tests, per RESEARCH.md Pitfall 2 / Open Question 1):** `COMPLETED = CURRENT` iff `Status:` line contains "phase complete" (case-insensitive), else `COMPLETED = CURRENT - 1` (floored at 0). Write two bats fixtures: one `Status: In progress`, one `Status: Phase complete — ready for verification`, asserting distinct integers.

**Portability constraint:** bash 3.2-safe — no `mapfile`, no `read -a`. Use `-E` (extended POSIX), never `-P` (GNU/PCRE-only, absent on BSD/macOS grep).

---

### `tests/consent-check.bats` (test, request-response)

**Analog:** `scripts/harness/ruflo-host-executor.bats`

**Setup/stub pattern** (lines 1-11, 28-41):
```bash
#!/usr/bin/env bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
HOST_EXECUTOR="$REPO_ROOT/scripts/harness/ruflo-host-executor.sh"

setup() {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  export PATH="$STUB_DIR:$PATH"
  ...
  cat > "$STUB_DIR/claude" <<'EOF'
#!/usr/bin/env bash
...
EOF
  chmod +x "$STUB_DIR/claude" ...
}
```
Adapt: stub `node_modules/.bin/gsd-tools` (not a PATH binary — RESEARCH.md Wave 0 Gaps recommends making `REPO_ROOT` resolution overridable via an env var like `CONSENT_CHECK_REPO_ROOT`, mirroring this file's `HOST_EXECUTOR_ROOT` override). Write a fake `gsd-tools` script at `$BATS_TEST_TMPDIR/node_modules/.bin/gsd-tools` that echoes a fixed JSON array (`capability list`) so tests are hermetic.

**Assertion pattern** (lines 64-70, 113-121):
```bash
@test "claude host runs from --cwd directory" {
  run bash "$HOST_EXECUTOR" --prompt-file "$PROMPT_FILE" --cwd "$TARGET_DIR" --host claude

  [ "$status" -eq 0 ]
  grep -Fx "pwd=$TARGET_PWD" "$HARNESS_RECORD"
}

@test "--cwd that escapes the containment root is rejected" {
  ...
  [ "$status" -eq 2 ]
  [[ "$output" == *"escapes containment root"* ]]
}
```
Adapt for consent-check: `run bash "$SCRIPT" active-cap-id`, assert `[ "$status" -eq 0 ]`; `run bash "$SCRIPT" missing-cap-id`, assert `[ "$status" -eq 1 ]` and `[[ "$output" == *"not active/consented"* ]]`; `run bash "$SCRIPT"` (no args), assert `[ "$status" -eq 2 ]` and usage message.

---

### `tests/state-phase.bats` (test, transform)

**Analog:** `scripts/harness/ruflo-host-executor.bats` (structural conventions only — no stubbing needed, per RESEARCH.md Wave 0 Gaps: "no stubbing needed; just write fixture STATE.md files to `$BATS_TEST_TMPDIR`")

**Structure to reuse:**
```bash
#!/usr/bin/env bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/gsd/state-phase.sh"

@test "mid-phase status returns current phase minus one" {
  FIXTURE="$BATS_TEST_TMPDIR/STATE.md"
  cat > "$FIXTURE" <<'EOF'
---
progress:
  completed_phases: 999
  percent: 999
---
## Current Position
Phase: 3 of 10
Status: In progress
EOF
  run bash "$SCRIPT" "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "2" ]
}

@test "missing file exits 2" {
  run bash "$SCRIPT" "$BATS_TEST_TMPDIR/nonexistent.md"
  [ "$status" -eq 2 ]
}
```
Note the frontmatter `999` values are deliberately wrong/absurd in the fixture — this is the regression-proof for "never reads frontmatter" (RESEARCH.md Anti-Patterns / Phase Requirements → Test Map row 3).

---

## Shared Patterns

### Strict mode + REPO_ROOT resolution
**Source:** `scripts/gsd/gates-test-command.sh` lines 1-6, `scripts/gsd/gsd-run.sh` lines 5,12
**Apply to:** both new scripts
```bash
#!/usr/bin/env bash
set -uo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```
Note: `set -uo pipefail` only (no `-e`) — matches all 3 existing `scripts/gsd/*.sh` files; do not add `-e`.

### Usage/exit-2 idiom
**Source:** `scripts/gsd/gsd-run.sh` lines 7-10
**Apply to:** both scripts, for argument-count validation
```bash
if [ $# -lt 1 ]; then
  echo "usage: <script-name> <args...>" >&2
  exit 2
fi
```

### bats stub-binary + tmpdir isolation
**Source:** `scripts/harness/ruflo-host-executor.bats` lines 8-11, 23 (`HOST_EXECUTOR_ROOT` override), 28-41 (heredoc stub)
**Apply to:** `tests/consent-check.bats` (stub `gsd-tools`); not needed for `tests/state-phase.bats` (pure fixture files)

### Fail-closed error handling
**Source:** RESEARCH.md Pattern 1/2, `scripts/gsd/gates-test-command.sh` lines 20-23, 35-38
**Apply to:** `consent-check.sh` only — every branch (missing node, missing binary, non-zero rc, unparsable JSON, capability absent, capability inactive) must resolve to `exit 1`, never `exit 0`. `state-phase.sh` has a different contract (`exit 2` for missing file, `exit 1` for unparsable body, `exit 0` + printed integer for success) — do not conflate the two exit-code schemes.

## No Analog Found

None — every file has at least a role-match structural analog in `scripts/gsd/*.sh` or `scripts/harness/*.bats`, and RESEARCH.md itself supplies verified, ready-to-copy content for both scripts' core logic (Patterns 1-3), so no file requires invented-from-scratch content.

## Metadata

**Analog search scope:** `scripts/gsd/*.sh`, `scripts/harness/*.bats`, `tests/*.py` (checked, no bash test precedent there — confirmed `.bats` is the right test format per RESEARCH.md)
**Files scanned:** `scripts/gsd/gates-test-command.sh`, `scripts/gsd/gsd-run.sh`, `scripts/gsd/review-gate-command.sh` (referenced in RESEARCH.md, not re-read — content already quoted verified there), `scripts/harness/ruflo-host-executor.bats`
**Pattern extraction date:** 2026-07-05
