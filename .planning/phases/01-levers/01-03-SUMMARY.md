---
phase: 01-levers
plan: 03
subsystem: testing
tags: [bash, bats, shellcheck, liveness, autonomy-gate, gates.py]

# Dependency graph
requires: []
provides:
  - "scripts/gsd/liveness-check.sh — composite AND-of-death autonomous-run liveness detector"
  - "CLI contract: liveness-check.sh <pidfile> <state-dir> [--run-id ID] [--window-min N]"
  - "env var contract: LIVENESS_WINDOW_MIN (default 30)"
affects: [adopt-wip, autonomy-grant, gsd-executor]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Composite AND-of-death liveness: ALIVE iff any of 3 signals holds; DEAD only on the all-false row"
    - "Reuse GATES_PY 3-candidate resolution + RUN_ID derivation verbatim across levers (review-gate-command.sh -> liveness-check.sh)"
    - "PATH-shim stubbing of an external binary (python3) in bats tests instead of mocking the script under test"
    - "Portable mtime freshness check via plain find + stat -f/-c fallback (no -newermt dependency — bfs/BSD/GNU find disagree on that flag)"

key-files:
  created:
    - scripts/gsd/liveness-check.sh
    - tests/bats/liveness-check.bats
  modified: []

key-decisions:
  - "Invented a self-contained pidfile+state-dir CLI contract instead of consuming lib/run_state (that SQLite CLI has no pid column — RESEARCH Pitfall 1)"
  - "G-signal test stubbing intercepts the python3 binary via PATH, not the gates.py file path, because a real gates.py already exists at the 2nd resolution candidate on dev machines — stubbing python3 decouples tests from which of the 3 candidates wins"
  - "mtime freshness implemented as enumerate-then-stat rather than find -newermt, after discovering this environment's `find` is bfs (not GNU/BSD) and rejects the '-N minutes' relative-time syntax the plan suggested"

requirements-completed: [REQ-11]

coverage:
  - id: D1
    description: "liveness-check.sh reports ALIVE (exit 0) iff pid-alive OR mtime-fresh OR ship:gsd-grant-in-flight, DEAD (exit 1) only on the FFF row"
    requirement: "REQ-11"
    verification:
      - kind: unit
        ref: "tests/bats/liveness-check.bats#8-row truth table (TTT..FFF)"
        status: pass
    human_judgment: false
  - id: D2
    description: "Garbage/missing pidfile inputs fail the pid signal to dead without crashing the script"
    requirement: "REQ-11"
    verification:
      - kind: unit
        ref: "tests/bats/liveness-check.bats#garbage pidfile / missing pidfile"
        status: pass
    human_judgment: false
  - id: D3
    description: "Script is shellcheck-clean"
    verification:
      - kind: unit
        ref: "shellcheck scripts/gsd/liveness-check.sh"
        status: pass
    human_judgment: false

duration: 25min
completed: 2026-07-10
status: complete
---

# Phase 01 Plan 03: Composite Liveness Detector Summary

**`scripts/gsd/liveness-check.sh` — an AND-of-death 3-signal liveness probe (pid/mtime/ship-grant) that only declares an autonomous run DEAD when all three signals agree, so one transient failed probe never kills an overnight wave.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-07-10T03:41Z (RED commit)
- **Completed:** 2026-07-10T03:42Z (GREEN commit)
- **Tasks:** 2 (TDD: RED, GREEN)
- **Files modified:** 2 (both new)

## Accomplishments
- Built `scripts/gsd/liveness-check.sh`: parses `<pidfile> <state-dir> [--run-id ID] [--window-min N]`, evaluates 3 independent signals (P=pid alive, M=mtime fresh, G=ship:gsd grant in flight), prints a per-signal verdict line for each, and exits 0 (ALIVE) unless all three are false (exit 1, DEAD).
- Reused the exact `GATES_PY` 3-candidate resolution and `RUN_ID` derivation pattern from `scripts/gsd/review-gate-command.sh` (lines 21-44) so both levers agree on where the autonomy-grant ledger lives — no new resolution scheme invented.
- Wrote `tests/bats/liveness-check.bats` covering all 8 rows of the P/M/G truth table plus garbage-pidfile and missing-pidfile edge cases, using the canary-gate.bats PATH-shim stub convention (stubbing `python3` itself rather than the gates.py file, since a real gates.py already resolves via the 2nd candidate on dev machines).
- Implemented the mtime freshness check portably: after `find -newermt "-N minutes"` failed on this environment's `find` (which resolves to `bfs`, not GNU/BSD find, and rejects relative-time syntax), replaced it with a plain `find -type f` enumeration + `stat -f %m`/`stat -c %Y` fallback dialect detection and integer arithmetic — bash-3.2-safe, no `mapfile`/`declare -A`.

## Task Commits

Each task was committed atomically (TDD RED/GREEN):

1. **Task 1: RED — liveness-check.bats 8-row truth table + garbage input** - `7ee7373` (test)
2. **Task 2: GREEN — liveness-check.sh composite AND-of-death signals** - `7b1160e` (feat)

**Plan metadata:** committed separately after this SUMMARY (see final commit).

## Files Created/Modified
- `scripts/gsd/liveness-check.sh` - composite AND-of-death liveness detector (140 lines, shellcheck-clean)
- `tests/bats/liveness-check.bats` - 8-row truth table + garbage/missing-pidfile bats coverage (10 tests, all passing)

## Decisions Made
- Invented a self-contained `<pidfile> <state-dir>` CLI contract rather than reusing `lib/run_state` (that's an unrelated SQLite CLI with no pid column — confirmed via RESEARCH Pitfall 1 and by reading `lib/gates.py`'s CLI).
- Stubbed the `python3` binary via PATH in tests (not the gates.py file) because this dev environment already has a real `gates.py` at the 2nd resolution candidate (`$HOME/.claude/lib/feature-fix-swarm/gates.py`), which would otherwise win over any 3rd-candidate test stub regardless of environment. Intercepting `python3` itself makes the G-signal fully deterministic and portable across machines that do or don't have a real gates.py installed.
- Replaced the plan's suggested `find -newermt "-N minutes"` with a portable stat-based mtime comparison after discovering this environment's `find` is `bfs` (not GNU/BSD find), which errors on relative ISO-adjacent time syntax. This is a Rule 1 (bug/portability) auto-fix — the behavior contract (M signal = fresh iff newest mtime within window) is unchanged, only the implementation mechanism differs from the plan's suggestion.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug/Portability] Replaced `find -newermt` with a portable stat-based mtime check**
- **Found during:** Task 2 (GREEN implementation)
- **Issue:** The plan's suggested `find "$state_dir" -type f -newermt "-N minutes"` fails on this environment because `find` resolves to `bfs` (a find replacement), which rejects GNU findutils' relative "-N minutes" timestamp syntax and errors instead of matching.
- **Fix:** Enumerate files with a plain `find -type f` (no timestamp flags — agreed upon by bfs/BSD/GNU find), then compute the max mtime via a `stat -f %m` (BSD/macOS) / `stat -c %Y` (GNU) fallback pair and compare with integer arithmetic against the window in seconds.
- **Files modified:** scripts/gsd/liveness-check.sh
- **Verification:** All 8 truth-table bats rows pass, including the fresh/stale mtime rows on this exact environment.
- **Committed in:** 7b1160e (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (portability bug in mtime detection)
**Impact on plan:** Necessary for correctness on this machine's toolchain; the documented behavior contract (M=fresh iff newest mtime within window-min) is unchanged.

## Issues Encountered
- Discovered mid-implementation that `$HOME/.claude/lib/feature-fix-swarm/gates.py` (the 2nd GATES_PY candidate) is a real, populated file on this dev machine — this would have silently defeated a naive "place a stub gates.py at the 3rd candidate" test approach. Resolved by stubbing `python3` itself via PATH instead, which is robust regardless of which candidate resolves.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- `scripts/gsd/liveness-check.sh` is ready for Phase 2 wiring into `adopt-wip`'s liveness consult (explicitly out of scope for this plan — no SKILL.md was touched, per the plan's prohibitions).
- CLI/env contract (`liveness-check.sh <pidfile> <state-dir> [--run-id ID] [--window-min N]`, `LIVENESS_WINDOW_MIN`) is stable and documented in the script header for downstream consumers.

---
*Phase: 01-levers*
*Completed: 2026-07-10*
