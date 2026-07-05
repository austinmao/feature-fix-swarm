---
phase: 01-support-scripts
plan: 02
subsystem: testing
tags: [bash, bats, shellcheck, state-parsing, gsd]

# Dependency graph
requires: []
provides:
  - "scripts/gsd/state-phase.sh — BODY-derived completed-phases integer from STATE.md"
  - "tests/state-phase.bats — hermetic fixture-based bats suite pinning the REQ-02 contract"
affects: [FFS preflight, gates automation, gsd phase-verification tooling]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "awk frontmatter-strip-before-grep: strip YAML frontmatter with a state-machine awk pass before any grep, so frontmatter counters/decoys are structurally unreadable rather than merely deprioritized"
    - "hermetic bats fixtures: fixture STATE.md files written per-test to $BATS_TEST_TMPDIR, no stubbing/PATH manipulation needed for pure text-transform scripts"

key-files:
  created:
    - scripts/gsd/state-phase.sh
    - tests/state-phase.bats
  modified: []

key-decisions:
  - "Completed-phase rule (A2, planner-locked): COMPLETED = CURRENT when body Status line contains 'phase complete' (case-insensitive), else CURRENT - 1 floored at 0 — proven by paired mid-phase/phase-complete fixtures"

patterns-established:
  - "Frontmatter-strip-before-parse: any future gsd-side STATE.md reader should copy this awk pattern rather than grep the raw file, to stay immune to the same frontmatter-counter unreliability class"

requirements-completed: [REQ-02]

coverage:
  - id: D1
    description: "state-phase.sh prints a body-derived completed-phases integer and exits 0 on a well-formed STATE.md"
    requirement: "REQ-02"
    verification:
      - kind: unit
        ref: "tests/state-phase.bats#mid-phase status returns current phase minus one (frontmatter poisoned with 999s)"
        status: pass
      - kind: unit
        ref: "tests/state-phase.bats#phase-complete status returns current phase unchanged"
        status: pass
      - kind: unit
        ref: "tests/state-phase.bats#first-phase mid-phase status floors at zero"
        status: pass
    human_judgment: false
  - id: D2
    description: "Frontmatter counters (completed_phases, percent) and a decoy in-frontmatter Phase line are structurally unreadable — body value always wins"
    requirement: "REQ-02"
    verification:
      - kind: unit
        ref: "tests/state-phase.bats#frontmatter decoy Phase line is stripped, body value wins"
        status: pass
    human_judgment: false
  - id: D3
    description: "Missing state file exits 2 with a 'not found' stderr message; body without a Phase line exits 1 with an actionable message; zero args defaults to .planning/STATE.md; two or more args exits 2 with a usage message"
    requirement: "REQ-02"
    verification:
      - kind: unit
        ref: "tests/state-phase.bats#missing state file exits 2 with not found message"
        status: pass
      - kind: unit
        ref: "tests/state-phase.bats#body without Phase line exits 1 with actionable message"
        status: pass
      - kind: unit
        ref: "tests/state-phase.bats#zero args defaults to .planning/STATE.md"
        status: pass
      - kind: unit
        ref: "tests/state-phase.bats#two or more args exits 2 with usage message"
        status: pass
    human_judgment: false
  - id: D4
    description: "Script is shellcheck-clean at -S warning, both standalone and as part of the repo-wide CI shellcheck glob"
    verification:
      - kind: other
        ref: "shellcheck -S warning scripts/gsd/state-phase.sh"
        status: pass
      - kind: other
        ref: "shellcheck -S warning setup.sh hooks/*.sh scripts/*.sh scripts/hooks/*.sh scripts/harness/*.sh scripts/gsd/*.sh"
        status: pass
    human_judgment: false

duration: 6min
completed: 2026-07-05
status: complete
---

# Phase 01 Plan 02: state-phase.sh Summary

**BODY-only STATE.md phase-progress parser (scripts/gsd/state-phase.sh) that routes around gsd-core's documented-unreliable frontmatter counters, TDD'd with an 8-test hermetic bats suite.**

## Performance

- **Duration:** 6 min
- **Started:** 2026-07-05T20:35:48Z
- **Completed:** 2026-07-05T20:41:48Z
- **Tasks:** 2
- **Files modified:** 2 (both created)

## Accomplishments
- `scripts/gsd/state-phase.sh` prints a single BODY-derived completed-phases integer, deriving CURRENT from the first `Phase: X of Y` line and applying the CURRENT/CURRENT-1 status tie-breaker, after an awk pass strips YAML frontmatter so `completed_phases`/`percent` and any decoy `Phase:`-shaped frontmatter line can never leak into the result.
- `tests/state-phase.bats` locks the full REQ-02 contract with 8 hermetic fixture tests (no stubbing, no PATH tricks) — poisoned frontmatter, decoy frontmatter Phase line, missing file, no-Phase-line body, default-arg, and two-arg usage error.
- Confirmed against the real project STATE.md: `bash scripts/gsd/state-phase.sh .planning/STATE.md` exits 0 and prints `0` (Phase 1 of 1, Status: Ready to execute → CURRENT-1 floored at 0).

## Task Commits

Each task was committed atomically:

1. **Task 1: RED — write tests/state-phase.bats** - `384fb14` (test)
2. **Task 2: GREEN — implement scripts/gsd/state-phase.sh** - `9cbb2c1` (feat)

_No REFACTOR commit — implementation copied verbatim from 01-PATTERNS.md's verified pattern; nothing needed cleanup._

## Files Created/Modified
- `scripts/gsd/state-phase.sh` - CLI transform: strips YAML frontmatter via awk, greps BODY for `Phase: X of Y` and `Status:` lines, applies the CURRENT/CURRENT-1 completed-phase rule, prints the integer
- `tests/state-phase.bats` - 8-test hermetic bats suite pinning exit codes, stderr message vocabulary, and the off-by-one rule via fixture pairs

## Decisions Made
- Followed the plan's Assumption A2 (planner-locked) verbatim: no independent decision needed — the paired mid-phase/phase-complete fixtures are the spec.
- No REFACTOR step: the RESEARCH.md Pattern 3 content was copy-ready and passed shellcheck/bats on first GREEN attempt, so no cleanup commit was warranted.

## Deviations from Plan

None - plan executed exactly as written. Implementation is a verbatim adaptation of `01-PATTERNS.md`'s "scripts/gsd/state-phase.sh" section plus the arg-count guard specified in the plan's `<implementation>` block.

## Issues Encountered
None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- REQ-02 fully satisfied: gsd-side phase-progress integer is now available to FFS preflight/gates automation via a shellcheck-clean, hermetically-tested script.
- Regression baseline unchanged: `python3 -m pytest lib/tests -q` still reports 190 passed.
- No blockers for downstream FFS work consuming this script's stdout contract.

---
*Phase: 01-support-scripts*
*Completed: 2026-07-05*

## Self-Check: PASSED

- FOUND: scripts/gsd/state-phase.sh
- FOUND: tests/state-phase.bats
- FOUND: .planning/phases/01-support-scripts/01-02-SUMMARY.md
- FOUND commit: 384fb14 (test)
- FOUND commit: 9cbb2c1 (feat)
- FOUND commit: bf86593 (docs)
