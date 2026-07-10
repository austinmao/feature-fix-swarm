---
phase: 02-skill-wiring
plan: 03
subsystem: testing
tags: [bats, skill-prose, liveness, harness-audit, gsd-debug]

requires:
  - phase: 01-levers
    provides: "frozen scripts/gsd/liveness-check.sh (01-03) and scripts/harness-audit.py (01-05)"
provides:
  - "adopt-wip Step 1 consults liveness-check.sh before declaring WIP abandoned (AC-011, INT-005)"
  - "preflight advisory Harness audit section invoking harness-audit.py, never blocks (AC-008)"
  - "fix diagnose routes to /gsd-debug on non-obvious repro with explicit criteria (AC-010, INT-006)"
  - "tests/bats/int-consult-levers.bats grep-pins for all three"
affects: [02-skill-wiring, phase-review-gate]

tech-stack:
  added: []
  patterns: ["grep-pin bats tests mirroring setup-install.bats static-assertion style"]

key-files:
  created:
    - tests/bats/int-consult-levers.bats
  modified:
    - skills/adopt-wip/SKILL.md
    - skills/preflight/SKILL.md
    - skills/fix/SKILL.md

key-decisions:
  - "Liveness consult ADDS to adopt-wip's existing mtime/process checks, does not replace them"
  - "Harness-audit section explicitly instructs never wiring its exit code/score into gates.py preflight (fail-closed manifest gate stays separate)"

patterns-established:
  - "Prose contract to frozen lever: grep-pin both the literal invocation AND the semantic wording (advisory/criteria) so future edits that keep the token but weaken the contract still trip"

requirements-completed: [REQ-08, REQ-10, REQ-11]

coverage:
  - id: D1
    description: "adopt-wip Step 1 consults liveness-check.sh before declaring WIP abandoned"
    requirement: "REQ-11"
    verification:
      - kind: integration
        ref: "tests/bats/int-consult-levers.bats#adopt-wip SKILL.md consults liveness-check.sh (INT-005)"
        status: pass
    human_judgment: false
  - id: D2
    description: "preflight gains advisory harness-audit.py section that never blocks"
    requirement: "REQ-08"
    verification:
      - kind: integration
        ref: "tests/bats/int-consult-levers.bats#preflight SKILL.md invokes harness-audit.py"
        status: pass
      - kind: integration
        ref: "tests/bats/int-consult-levers.bats#preflight harness-audit section is documented advisory, never-blocks"
        status: pass
    human_judgment: false
  - id: D3
    description: "fix diagnose routes to /gsd-debug when no failing test AND no single-command repro"
    requirement: "REQ-10"
    verification:
      - kind: integration
        ref: "tests/bats/int-consult-levers.bats#fix SKILL.md routes to /gsd-debug on non-obvious repro (INT-006)"
        status: pass
      - kind: integration
        ref: "tests/bats/int-consult-levers.bats#fix SKILL.md documents the explicit no-failing-test AND no-single-command-repro criteria"
        status: pass
    human_judgment: false

duration: 20min
completed: 2026-07-10
status: complete
---

# Phase 02 Plan 03: Consult-Lever Skill Wiring Summary

**Three prose contracts wired to frozen levers — adopt-wip liveness-check.sh, preflight advisory harness-audit.py, fix's /gsd-debug routing — all RED-first via a new int-consult-levers.bats.**

## Performance

- **Duration:** ~20 min
- **Tasks:** 2 (both TDD RED->GREEN)
- **Files modified:** 4 (1 new, 3 edited)

## Accomplishments
- `tests/bats/int-consult-levers.bats` created with 5 grep-pin `@test`s, confirmed RED (all 5 failing) before any prose edit.
- `skills/adopt-wip/SKILL.md` Step 1 gains a composite liveness-check.sh consult (P||M||G, exit 1 DEAD = adoptable, exit 0 ALIVE = WAIT) alongside — not replacing — the existing mtime/process/WAIT checks. version 1.0.0 -> 1.1.0.
- `skills/preflight/SKILL.md` gains a `### Harness audit (advisory)` section invoking `python3 scripts/harness-audit.py`, explicitly documented as never-blocking (always exit 0, EDGE-007 skip-note honored). version 1.0.0 -> 1.1.0.
- `skills/fix/SKILL.md` Step 1 gains explicit routing to `/gsd-debug` when (no failing test yet) AND (no single-command repro exists); otherwise stays on `/investigate` + `/gsd-quick`. version 3.1.0 -> 3.2.0.

## Task Commits

1. **RED — int-consult-levers.bats** - `c978331` (test)
2. **Task 1 GREEN — adopt-wip + preflight** - `106b7c4` (feat)
3. **Task 2 GREEN — fix /gsd-debug routing** - `3bba7cf` (feat)

## Files Created/Modified
- `tests/bats/int-consult-levers.bats` - 5 grep-pin tests (INT-005, harness-audit x2, INT-006, criteria wording)
- `skills/adopt-wip/SKILL.md` - liveness-check.sh consult added to Step 1; version 1.1.0
- `skills/preflight/SKILL.md` - advisory Harness audit section; version 1.1.0
- `skills/fix/SKILL.md` - /gsd-debug routing criteria in Step 1; version 3.2.0

## Decisions Made
- Kept the liveness consult additive per the plan's explicit prohibition (do not remove adopt-wip's existing stale-checks) — worded as a fourth bullet under Step 1, not a replacement of the first two.
- Preflight section explicitly tells the reader NOT to wire harness-audit's exit code/score into `gates.py preflight`, to keep the advisory/fail-closed-manifest boundary unambiguous for future editors.

## Deviations from Plan

None — plan executed exactly as written. No levers touched (liveness-check.sh, harness-audit.py untouched — grep-verified via `git status` before/after).

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All three consult-lever prose contracts wired and pinned; no known gaps for REQ-08/10/11's skill-wiring halves.
- No blockers for sibling plans 02-01/02-02 (disjoint files; 02-02 already landed on top with no conflicts observed at commit time).

---
*Phase: 02-skill-wiring*
*Completed: 2026-07-10*
