---
phase: 02-skill-wiring
plan: 02
subsystem: testing
tags: [skill-prose, gsd, learnings-harvest, code-uplift, feature-implement, bats]

requires: []
provides:
  - "feature-implement + code-uplift finish tails both invoke the fail-soft learnings-harvest.sh lever"
  - "code-uplift --slop-only deslop fast path (green-baseline WALL + diff-scoped deletion-first cleanup)"
  - "tests/bats/int-finish-tail.bats INT-004 + AC-009 grep-pins"
affects: [02-01-PLAN, 02-03-PLAN]

tech-stack:
  added: []
  patterns:
    - "Grep-pin bats tests over committed SKILL.md prose (mirrors tests/bats/setup-install.bats) — no script execution"
    - "--slop-only documented as a skill-prose invocation mode, not a new lever"

key-files:
  created:
    - tests/bats/int-finish-tail.bats
  modified:
    - skills/feature-implement/SKILL.md
    - skills/code-uplift/SKILL.md

key-decisions:
  - "Learnings step placed after review-gate/ship/canary in feature-implement Step 6 (run-end capture, count reported in Step 7) rather than earlier in the tail"
  - "code-uplift's grep-pin satisfied by naming learnings-harvest.sh literally in Step 4 prose instead of duplicating the full step text"
  - "--slop-only lands as a new Step 0b section (before Step 1 review sweep) so it can run before /review-gate ever sees the diff"

patterns-established:
  - "Finish-tail-owning skills each literally name any shared lever they invoke, so per-skill grep-pins hold independently"

requirements-completed: [REQ-03, REQ-09]

coverage:
  - id: D1
    description: "feature-implement Step 6 finish tail invokes fail-soft learnings-harvest.sh (always exit 0), version bumped 2.3.0 -> 2.4.0"
    requirement: "REQ-03"
    verification:
      - kind: unit
        ref: "tests/bats/int-finish-tail.bats#feature-implement finish tail invokes learnings-harvest.sh"
        status: pass
    human_judgment: false
  - id: D2
    description: "code-uplift Step 4 finish tail names the same learnings-harvest.sh lever literally"
    requirement: "REQ-03"
    verification:
      - kind: unit
        ref: "tests/bats/int-finish-tail.bats#code-uplift finish tail invokes learnings-harvest.sh"
        status: pass
    human_judgment: false
  - id: D3
    description: "code-uplift --slop-only fast path documented: green-baseline WALL, deletion-first, diff-scoped, net-delta report, EDGE-008 empty-diff no-op; version bumped 1.0.0 -> 1.1.0"
    requirement: "REQ-09"
    verification:
      - kind: unit
        ref: "tests/bats/int-finish-tail.bats#code-uplift documents the --slop-only fast path"
        status: pass
      - kind: unit
        ref: "tests/bats/int-finish-tail.bats#code-uplift --slop-only documents the green-baseline refusal wall"
        status: pass
    human_judgment: false

duration: 15min
completed: 2026-07-10
status: complete
---

# Phase 02 Plan 02: Finish-tail learnings-harvest wiring + code-uplift --slop-only Summary

**Both finish-tail-owning skills (feature-implement, code-uplift) now literally invoke the fail-soft `learnings-harvest.sh` lever, and code-uplift gains a documented `--slop-only <diff-base>` deslop fast path with a green-baseline refusal wall.**

## Performance

- **Duration:** 15 min
- **Tasks:** 2 (both TDD, RED then GREEN)
- **Files modified:** 3 (1 new bats file, 2 SKILL.md edits)

## Accomplishments
- `tests/bats/int-finish-tail.bats` created with 4 RED-first grep-pins (INT-004 x2, AC-009 x2)
- feature-implement Step 6 finish tail names `scripts/gsd/learnings-harvest.sh` as a fail-soft, run-end step; version 2.3.0 -> 2.4.0
- code-uplift Step 4 finish tail names the same lever literally (was only "Identical to Step 6" prose)
- code-uplift gains new Step 0b `--slop-only` fast path: green-baseline WALL, deletion-first, diff-scoped, net-delta report, EDGE-008 empty-diff no-op; version 1.0.0 -> 1.1.0

## Task Commits

Each task committed atomically, RED then GREEN:

1. **Task 1 RED** - `b0f8a99` (test: failing learnings-harvest.sh pins for both skills)
2. **Task 1 GREEN** - `a869cca` (feat: wire learnings-harvest.sh into both finish tails)
3. **Task 2 RED** - `af5a12e` (test: failing --slop-only + green-baseline-wall pins)
4. **Task 2 GREEN** - `b88f75c` (feat: document code-uplift --slop-only deslop fast path)

_All 4 tasks/gates confirmed RED before their corresponding GREEN edit (verified via `bats` run showing explicit failures before each feat commit)._

## Files Created/Modified
- `tests/bats/int-finish-tail.bats` - 4 grep-pin @tests (INT-004 learnings-harvest.sh x2, AC-009 --slop-only + green-baseline-wall x2)
- `skills/feature-implement/SKILL.md` - Step 6 gains learnings-harvest step; version 2.4.0
- `skills/code-uplift/SKILL.md` - Step 4 names learnings-harvest.sh; new Step 0b --slop-only section; version 1.1.0

## Decisions Made
- Learnings step placed at the end of feature-implement's finish tail (after ship/canary) since it's a run-end capture, with its count folded into the Step 7 report — not inserted earlier where it could block ship-adjacent steps.
- code-uplift's INT-004 pin satisfied with a single added clause rather than restating the whole learnings step, keeping "Identical to Step 6" as the source of truth and only literalizing the lever name for the grep-pin.
- --slop-only implemented purely as skill prose (Step 0b) per the hard constraint — no new script, `scripts/gsd/learnings-harvest.sh` untouched.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Both finish-tail skills and the shared bats coverage are in place for 02-01/02-03 to build on if they touch adjacent finish-tail prose.
- No blockers.

---
*Phase: 02-skill-wiring*
*Completed: 2026-07-10*
