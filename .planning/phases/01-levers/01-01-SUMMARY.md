---
phase: 01-levers
plan: 01
subsystem: infra
tags: [bash, python3, bats, shellcheck, pretooluse-hook, delegation]

# Dependency graph
requires: []
provides:
  - "scripts/hooks/delegation-enforcer.sh — PreToolUse hook auto-pinning unpinned Agent/Task spawns from .planning/config.json model_overrides"
  - "DELEGATION_ENFORCER=off kill-switch env var contract"
  - "tests/bats/delegation-enforcer.bats — happy/error path bats coverage"
affects: [phase-02-hook-registration, phase-03-delegation-lever-rollout]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "PreToolUse fail-open hook: bash set -euo pipefail wrapper + python3 -<<'PY' heredoc, INPUT=$(cat), json.loads in try/except, sys.exit(0) on any failure (mirrors scripts/hooks/gsd-phase-evidence-gate.sh)"

key-files:
  created:
    - scripts/hooks/delegation-enforcer.sh
    - tests/bats/delegation-enforcer.bats
  modified: []

key-decisions:
  - "Injected result is the full input envelope with tool_input.model set (PATH-001 shape), NOT wrapped in hookSpecificOutput.updatedInput — RESEARCH Open Question 1 decision; live-registration contract to be confirmed in Phase 2/3, not this phase"
  - "bats run captures stdout+stderr merged by default; switched the two warn-path tests to run --separate-stderr so the stdout byte-identity assertion isn't broken by the stderr WARN line (test bug fix, not a script behavior change)"

patterns-established:
  - "Advisory PreToolUse hooks always exit 0; fail-open on any parse/config error; kill-switch via env var checked before any parsing"

requirements-completed: [REQ-01, REQ-02]

coverage:
  - id: D1
    description: "Unpinned Agent/Task JSON + seeded config_overrides -> tool_input.model injected"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/bats/delegation-enforcer.bats#PATH-001: unpinned Agent JSON + seeded config -> injects tool_input.model"
        status: pass
    human_judgment: false
  - id: D2
    description: "Already-pinned Agent JSON -> byte-identical passthrough"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/bats/delegation-enforcer.bats#already-pinned Agent JSON -> byte-identical passthrough"
        status: pass
    human_judgment: false
  - id: D3
    description: "No .planning/config.json -> passthrough + stderr warn"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/bats/delegation-enforcer.bats#no .planning/config.json -> passthrough + non-empty stderr warn"
        status: pass
    human_judgment: false
  - id: D4
    description: "config.json present but no model_overrides key -> passthrough + warn"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/bats/delegation-enforcer.bats#EDGE-002: config.json present but no model_overrides key -> passthrough + warn"
        status: pass
    human_judgment: false
  - id: D5
    description: "Non-Agent tool JSON (Bash) -> byte-identical passthrough, no parse beyond tool-name check"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/bats/delegation-enforcer.bats#EDGE-001: non-Agent tool JSON (Bash) -> byte-identical passthrough"
        status: pass
    human_judgment: false
  - id: D6
    description: "DELEGATION_ENFORCER=off -> unconditional passthrough"
    requirement: "REQ-02"
    verification:
      - kind: unit
        ref: "tests/bats/delegation-enforcer.bats#AC-002: DELEGATION_ENFORCER=off -> unconditional passthrough"
        status: pass
    human_judgment: false
  - id: D7
    description: "Malformed/hostile JSON on stdin -> passthrough, never crash (fail-open)"
    requirement: "REQ-01"
    verification:
      - kind: unit
        ref: "tests/bats/delegation-enforcer.bats#malformed JSON on stdin -> passthrough, never crash"
        status: pass
    human_judgment: false

# Metrics
duration: 3min
completed: 2026-07-10
status: complete
---

# Phase 01 Plan 01: Delegation Enforcer Hook Summary

**PreToolUse hook (`scripts/hooks/delegation-enforcer.sh`) that auto-pins unpinned Agent/Task spawns to the model resolved from `.planning/config.json` `model_overrides[subagent_type]`, fail-open and advisory-only (always exit 0).**

## Performance

- **Duration:** 3 min
- **Started:** 2026-07-10T03:36:35Z
- **Completed:** 2026-07-10T03:38:51Z
- **Tasks:** 2 (TDD: RED + GREEN)
- **Files modified:** 2

## Accomplishments
- `scripts/hooks/delegation-enforcer.sh` — reads Agent/Task tool_use JSON on stdin, injects `tool_input.model` from config when unpinned, else passes through byte-identical
- `DELEGATION_ENFORCER=off` kill-switch for unconditional passthrough (AC-002)
- Fail-open on malformed JSON, missing config, and missing `model_overrides` key — never crashes, never blocks a spawn
- `tests/bats/delegation-enforcer.bats` — 7 `@test` blocks covering PATH-001, pinned passthrough, no-config warn, EDGE-002, EDGE-001, AC-002, and malformed-JSON fail-open

## Task Commits

Each task was committed atomically:

1. **Task 1: RED — delegation-enforcer.bats covering inject / passthrough / off / fail-open** - `9ce3e25` (test)
2. **Task 2: GREEN — delegation-enforcer.sh (fail-open, advisory model injection)** - `b62db16` (feat)

_Note: TDD gate sequence confirmed: `test(01-01)` commit precedes `feat(01-01)` commit; no refactor commit needed._

## Files Created/Modified
- `scripts/hooks/delegation-enforcer.sh` - new PreToolUse hook: fail-open, advisory model auto-pin
- `tests/bats/delegation-enforcer.bats` - new bats file: 7 happy/error path tests

## Decisions Made
- Injected result mirrors the PATH-001 stub literally (full input envelope with `tool_input.model` set) rather than a `hookSpecificOutput.updatedInput` wrapper, per the plan's resolved Open Question 1 — deferred to Phase 2/3 live-registration wiring.
- Fixed a test-authoring bug (not a script bug): bats `run` merges stdout+stderr by default, which broke the byte-identity assertion on the two warn-path tests. Switched those two tests to `run --separate-stderr` (bats-core >=1.5.0, available here at 1.13.0) and added `bats_require_minimum_version 1.5.0`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed bats test bug: stderr merge broke stdout byte-identity assertion**
- **Found during:** Task 2 (GREEN implementation) — running the RED-authored bats file against the finished script
- **Issue:** `run bash "$HOOK" <<<"$INPUT"` captures stdout+stderr combined by default in bats-core; the no-config and EDGE-002 tests assert `stderr` warns AND `$output == $INPUT`, but the merged stream broke the equality check even though the script's stdout was correct
- **Fix:** Switched those two `@test` blocks to `run --separate-stderr`, added `bats_require_minimum_version 1.5.0`
- **Files modified:** tests/bats/delegation-enforcer.bats
- **Verification:** `bats tests/bats/delegation-enforcer.bats` — 7/7 pass
- **Committed in:** `b62db16` (part of Task 2 commit, alongside the script)

---

**Total deviations:** 1 auto-fixed (test bug, no script behavior change)
**Impact on plan:** No scope creep — script behavior matches plan exactly; only a test-harness assertion technique changed.

## Issues Encountered
None beyond the deviation above.

## User Setup Required
None - no external service configuration required. Hook registration in `.claude/settings.json` is explicitly out of scope for this plan (consumer-repo action, Phase 2+).

## Next Phase Readiness
- `scripts/hooks/delegation-enforcer.sh` is ready to be wired into `.claude/settings.json` PreToolUse hooks in a later phase (registration deliberately excluded here per plan prohibitions).
- Live Claude Code `hookSpecificOutput.updatedInput` contract still needs confirmation before registration (Open Question 1 follow-up).

---
*Phase: 01-levers*
*Completed: 2026-07-10*

## Self-Check: PASSED

- FOUND: scripts/hooks/delegation-enforcer.sh
- FOUND: tests/bats/delegation-enforcer.bats
- FOUND: .planning/phases/01-levers/01-01-SUMMARY.md
- FOUND commit: 9ce3e25 (test)
- FOUND commit: b62db16 (feat)
- FOUND commit: 1993fff (docs)
