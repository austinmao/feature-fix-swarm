---
phase: 01-support-scripts
plan: 01
subsystem: testing
tags: [bash, bats, shellcheck, node, ci, gsd-tools]

requires: []
provides:
  - "scripts/gsd/consent-check.sh: fail-closed capability/consent assertion CLI"
  - "tests/consent-check.bats: hermetic bats suite pinning the full exit-code/message contract"
  - "CI shellcheck job now lints scripts/gsd/*.sh"
affects: [01-02, ffs-preflight, ffs-gates]

tech-stack:
  added: []
  patterns:
    - "Fail-closed subprocess wrapper: every non-positive verification path resolves to exit 1, never exit 0"
    - "argv-safe node -e JSON parse: untrusted id passed via process.argv, never interpolated into the -e source string"

key-files:
  created:
    - scripts/gsd/consent-check.sh
    - tests/consent-check.bats
  modified:
    - .github/workflows/ci.yml

key-decisions:
  - "Adopted 01-PATTERNS.md Pattern 1/2 verbatim (fail-closed dependency checks + argv-safe node -e JSON parse) rather than inventing new logic"
  - "CI shellcheck glob extended by exactly one token (scripts/gsd/*.sh) per RESEARCH Pitfall 1 / Assumption A3"

patterns-established:
  - "consent-check.sh house style: #!/usr/bin/env bash, set -uo pipefail (no -e), bash-3.2-safe, all expansions double-quoted, stderr prefixed 'consent-check:'"

requirements-completed: [REQ-01]

coverage:
  - id: D1
    description: "consent-check.sh <capability-id> exits 0 iff gsd-tools capability list reports the id with status active"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/consent-check.bats#active capability id exits 0"
        status: pass
    human_judgment: false
  - id: D2
    description: "consent-check.sh fails closed (exit 1, actionable stderr) on absent id, inactive status, missing node, missing gsd-tools binary, non-zero gsd-tools exit, or unparsable JSON"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/consent-check.bats#absent capability id exits 1 with not active/consented message and the id"
        status: pass
      - kind: unit
        ref: "tests/consent-check.bats#present but inactive capability exits 1"
        status: pass
      - kind: unit
        ref: "tests/consent-check.bats#gsd-tools stub exiting non-zero exits 1 and fails closed"
        status: pass
      - kind: unit
        ref: "tests/consent-check.bats#missing gsd-tools binary exits 1 and fails closed"
        status: pass
    human_judgment: false
  - id: D3
    description: "consent-check.sh with zero or two+ args exits 2 with a usage message on stderr"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/consent-check.bats#zero args exits 2 with usage message"
        status: pass
      - kind: unit
        ref: "tests/consent-check.bats#two args exits 2 with usage message"
        status: pass
    human_judgment: false
  - id: D4
    description: "consent-check.sh is shellcheck -S warning clean and scripts/gsd/*.sh is now linted in CI"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "shellcheck -S warning scripts/gsd/consent-check.sh (invocation, this run)"
        status: pass
      - kind: unit
        ref: "shellcheck -S warning setup.sh hooks/*.sh scripts/*.sh scripts/hooks/*.sh scripts/harness/*.sh scripts/gsd/*.sh (invocation, this run)"
        status: pass
    human_judgment: false

duration: 5min
completed: 2026-07-05
status: complete
---

# Phase 01 Plan 01: consent-check.sh Summary

**Fail-closed `scripts/gsd/consent-check.sh` CLI that queries `gsd-tools capability list` and asserts a capability id is active, backed by a 7-test hermetic bats suite and a CI shellcheck glob extension.**

## Performance

- **Duration:** 5 min
- **Started:** 2026-07-05T20:37:17Z
- **Completed:** 2026-07-05T20:40:10Z
- **Tasks:** 3
- **Files modified:** 3 (2 created, 1 modified)

## Accomplishments
- `scripts/gsd/consent-check.sh`: exit 0 (active) / exit 1 fail-closed (absent, inactive, missing node, missing gsd-tools binary, non-zero rc, unparsable JSON) / exit 2 (usage)
- `tests/consent-check.bats`: 7 hermetic tests, stubbing `node_modules/.bin/gsd-tools` as a Node JS file invoked via `node <path>`
- CI shellcheck job now covers `scripts/gsd/*.sh`

## Task Commits

Each task was committed atomically (TDD: RED then GREEN):

1. **Task 1: RED — write tests/consent-check.bats** - `72acea7` (test)
2. **Task 2: GREEN — implement scripts/gsd/consent-check.sh** - `46e1ead` (feat)
3. **Task 3: Add scripts/gsd/*.sh to the CI shellcheck glob** - `8ad90dd` (chore)

_No refactor commit needed — implementation passed shellcheck and bats on first GREEN pass._

## Files Created/Modified
- `scripts/gsd/consent-check.sh` - fail-closed capability/consent assertion CLI; delegates to `node node_modules/.bin/gsd-tools capability list`, parses via argv-safe `node -e`
- `tests/consent-check.bats` - 7-test hermetic bats suite pinning the exit-code and stderr-message contract
- `.github/workflows/ci.yml` - shellcheck job glob gains `scripts/gsd/*.sh` (single-token change, one line touched)

## Decisions Made
- Adopted 01-PATTERNS.md Pattern 1 (fail-closed dependency checks) and Pattern 2 (argv-safe `node -e` JSON parse) verbatim as specified in the plan — no deviation.
- Capability id enters the `node -e` JS exclusively via `process.argv[1]`; the `-e` source string is a fixed single-quoted literal with zero shell expansions (T-01-01 mitigation, verified by grep and by Test 1-3 exercising the argv path).

## Deviations from Plan

None - plan executed exactly as written. All three tasks matched their `<action>` specs; no Rule 1-4 auto-fixes were needed.

## Issues Encountered

None. `bash scripts/gsd/consent-check.sh ffs-gates` exits 1 as expected — in this worktree `node_modules/.bin/gsd-tools` is not present at all (rather than present-but-lacking-the-id, as the plan's real-machine baseline described), so the specific stderr message is "gsd-tools binary not found" instead of "not active/consented"; both are exit-1 fail-closed branches explicitly required by REQ-01 and both are pinned by the bats suite (Tests 2 and 5), so this is not a deviation from the contract.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- REQ-01 fully satisfied: `consent-check.sh` is available for FFS preflight/gates to call for a deterministic yes/no on gsd-side consent state.
- `python3 -m pytest lib/tests -q` regression baseline unchanged: 190 passed.
- Plan 01-02 (state-phase.sh) can proceed independently — no shared state introduced by this plan beyond the new script and its test.

---
*Phase: 01-support-scripts*
*Completed: 2026-07-05*

## Self-Check: PASSED

- FOUND: scripts/gsd/consent-check.sh
- FOUND: tests/consent-check.bats
- FOUND: .planning/phases/01-support-scripts/01-01-SUMMARY.md
- FOUND: 72acea7 (test)
- FOUND: 46e1ead (feat)
- FOUND: 8ad90dd (chore)
- FOUND: 14d1da9 (docs)
