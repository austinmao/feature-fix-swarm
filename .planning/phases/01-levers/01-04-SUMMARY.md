---
phase: 01-levers
plan: 04
subsystem: testing
tags: [bash, bats, gbrain, jsonl, tdd, fail-soft]

requires: []
provides:
  - "scripts/gsd/learnings-harvest.sh: fail-soft gbrain-or-archive learnings harvester (always exit 0)"
  - "tests/bats/learnings-harvest.bats: 5-scenario bats coverage (healthy backend, no-gbrain fallback, unreachable-gbrain fallback, malformed-line skip, empty/missing)"
affects: [phase-2-finish-tail-wiring]

tech-stack:
  added: []
  patterns:
    - "gbrain wrapper `gb() { env -u DATABASE_URL gbrain \"$@\"; }` + healthy-probe via `gbrain doctor`, mirrored from scripts/gsd/mempalace"
    - "MINPATH bats fixture: symlink only needed core utils, deliberately omit an external bin under test to simulate its absence even when the real binary is on the dev host's PATH"

key-files:
  created:
    - scripts/gsd/learnings-harvest.sh
    - tests/bats/learnings-harvest.bats
  modified: []

key-decisions:
  - "gbrain write shape: per-entry `gb put learnings/<sha256-12> <entry>` then a single `gb sync --no-pull --no-embed`, matching the existing scripts/gsd/mempalace convention rather than inventing a new bulk subcommand — keeps this lever consistent with the repo's one real gbrain integration point."
  - "Archive fallback uses flock-guarded append (fd 9 on the archive file itself) when flock is available, plain append otherwise (this dev machine has no flock) — matches the RESEARCH Pattern 5 atomic-append guidance without requiring a separate lock file."
  - "Bats fixtures build an explicit MINPATH (symlinked core utils only) rather than relying on ambient PATH lacking gbrain, because this repo's real gbrain binary IS on the executing machine's PATH (~/.bun/bin/gbrain) and would silently make the 'no gbrain' and 'malformed line' tests false-pass without this isolation."

requirements-completed: [REQ-03]

coverage:
  - id: D1
    description: "learnings-harvest.sh globs .planning/**/learnings*.jsonl, validates each line as JSON, and skips+counts malformed lines without aborting (EDGE-006)"
    requirement: "REQ-03"
    verification:
      - kind: unit
        ref: "tests/bats/learnings-harvest.bats#malformed JSONL line mixed with valid: valid harvested, malformed skipped, exit 0 (EDGE-006)"
        status: pass
    human_judgment: false
  - id: D2
    description: "Healthy gbrain on PATH receives distilled entries via gb put + gb sync, and the count is reported (PATH-002)"
    requirement: "REQ-03"
    verification:
      - kind: unit
        ref: "tests/bats/learnings-harvest.bats#healthy gbrain on PATH: N entries harvested, backend actually invoked"
        status: pass
    human_judgment: false
  - id: D3
    description: "gbrain absent or unreachable falls back to atomic append into .feature-fix-swarm/learnings-archive.jsonl, still exit 0 (AC-003 fallback, PATH-002 unreachable path)"
    requirement: "REQ-03"
    verification:
      - kind: unit
        ref: "tests/bats/learnings-harvest.bats#no gbrain on PATH: entries appended atomically to archive fallback"
        status: pass
      - kind: unit
        ref: "tests/bats/learnings-harvest.bats#gbrain present but unreachable (doctor fails): warn + archive fallback, exit 0"
        status: pass
    human_judgment: false
  - id: D4
    description: "Zero entries / missing .planning exits 0 and prints '0 harvested'"
    requirement: "REQ-03"
    verification:
      - kind: unit
        ref: "tests/bats/learnings-harvest.bats#zero entries / missing .planning: exit 0, 0 harvested"
        status: pass
    human_judgment: false
  - id: D5
    description: "Script is shellcheck-clean and bash-3.2-safe (no mapfile/declare -A/globstar)"
    requirement: "REQ-03"
    verification:
      - kind: unit
        ref: "shellcheck scripts/gsd/learnings-harvest.sh (exit 0, no findings)"
        status: pass
    human_judgment: false

duration: 20min
completed: 2026-07-10
status: complete
---

# Phase 01 Plan 04: Learnings Harvest Lever Summary

**Fail-soft `scripts/gsd/learnings-harvest.sh` that globs `.planning/**/learnings*.jsonl`, writes valid entries to gbrain via `gb put`+`gb sync` when the backend is healthy, else atomically appends them to `.feature-fix-swarm/learnings-archive.jsonl` — always exits 0, always prints `<N> harvested`.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-07-10T03:35:00Z (approx)
- **Completed:** 2026-07-10T03:43:19Z
- **Tasks:** 2 (RED + GREEN)
- **Files modified:** 2 (both new)

## Accomplishments
- Built the REQ-03 lever half: a standalone, always-exit-0 learnings harvester with no dependency on any finish-tail wiring (that's Phase 2).
- Backend probe mirrors the repo's memory-routing discipline exactly: `command -v gbrain && env -u DATABASE_URL gbrain doctor`.
- Archive fallback path is atomic (flock-guarded when available) and creates `.feature-fix-swarm/` on demand.
- Malformed-line handling (EDGE-006) is a per-line JSON validation pass (jq, python3 fallback) that counts and skips without aborting the harvest.
- Full bats suite (69 tests across the repo) green after this plan's addition — no regressions against the 64-test baseline.

## Task Commits

Each task was committed atomically:

1. **Task 1: RED — learnings-harvest.bats (backend stub, archive fallback, empty, malformed)** - `15e33e0` (test)
2. **Task 2: GREEN — learnings-harvest.sh (gbrain-or-archive, always exit 0)** - `1dbf9fe` (feat)

**Plan metadata:** committed together with this SUMMARY (see final commit in this plan's worktree branch).

_Note: TDD plan — RED then GREEN, no refactor commit needed (implementation was minimal and clean on first pass)._

## Files Created/Modified
- `scripts/gsd/learnings-harvest.sh` - Fail-soft learnings harvester: glob → validate → gbrain-probe → write-or-archive → always exit 0
- `tests/bats/learnings-harvest.bats` - 5 bats scenarios covering healthy backend, no-gbrain fallback, unreachable-gbrain fallback, malformed-line skip (EDGE-006), and empty/missing-planning

## Decisions Made
- gbrain write shape follows the existing `scripts/gsd/mempalace` convention (`gb put <key> <value>` per entry, then one `gb sync --no-pull --no-embed`) rather than inventing a bulk ingestion subcommand — keeps the repo's only two gbrain integration points consistent, and this is the real command shape that will work when Phase 2 wires the finish tails to actually call it.
- Bats tests build an explicit MINPATH (core utils symlinked, `gbrain` deliberately omitted) rather than trusting the ambient PATH lacks gbrain — this dev machine has a real `gbrain` binary on PATH (`~/.bun/bin/gbrain`), which would have silently broken the "no gbrain" and "malformed line" (archive-path) test isolation without this guard.
- Archive append uses `flock` on the archive file's own fd when `flock` is available (this dev machine doesn't have it — plain append is the fallback path exercised by every passing test here), matching the "append is not a tmp+mv replace" plan.md guidance.

## Deviations from Plan

None — plan executed exactly as written. The plan's interface notes (fail-soft convention, backend-probe shape, atomic-append pattern) were followed directly; the only design choice not explicitly dictated by the plan was the concrete gbrain write subcommand shape, which was resolved by mirroring the existing `scripts/gsd/mempalace` script (same repo, same integration point) rather than inventing a new contract.

## Issues Encountered
- The dev machine running this plan has a real `gbrain` binary on PATH (confirmed via `command -v gbrain` inside a bash subprocess), which would have made the "no gbrain" and "unreachable gbrain" bats scenarios non-hermetic if PATH were left unmodified. Resolved by building an explicit MINPATH per test (symlinking only the utils the script needs) instead of assuming an empty/minimal ambient PATH.

## User Setup Required

None - no external service configuration required. (Real gbrain wiring happens in Phase 2's finish-tail integration; this plan only builds and tests the standalone lever.)

## Next Phase Readiness
- `scripts/gsd/learnings-harvest.sh` is ready to be invoked from the `feature-implement` and `code-uplift` finish tails in Phase 2 — no changes to those SKILL.md files were made or needed in this plan (explicitly out of scope per the plan's prohibitions).
- No blockers. Script is standalone, has no dependency on other Phase 1 plans (wave 1, `depends_on: []`).

---
*Phase: 01-levers*
*Completed: 2026-07-10*
