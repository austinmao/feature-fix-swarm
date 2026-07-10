---
phase: 01-levers
plan: 02
subsystem: testing
tags: [bash, bats, shellcheck, review-gate, git-diff, security]

requires: []
provides:
  - "scripts/gsd/security-surface.sh — one sourceable home for the security-surface KEYWORDS pattern"
  - "scripts/gsd/review-tier.sh — diff risk-tier detector (light|standard|full) for review-gate sizing"
affects: [01-01, 01-03, 01-04, 01-05, 01-06]

tech-stack:
  added: []
  patterns:
    - "sourceable bash lib convention (header doc + no side effects on source), mirroring adversary-host.sh"
    - "path-component/token-boundary keyword matching via `tr '/._-' '    ' | grep -Ewi` to avoid substring false-positives (docs/authoring.md vs auth)"
    - "fail-safe git-metadata handling: every git call's exit status explicitly checked, any failure routes to the safe/conservative branch"

key-files:
  created:
    - scripts/gsd/security-surface.sh
    - scripts/gsd/review-tier.sh
    - tests/bats/review-tier.bats
  modified:
    - scripts/gsd/security-model-fence.sh

key-decisions:
  - "review-tier full-tier reasons distinguish security-surface path / migration path / file-count so a human can see WHY a diff was upgraded"
  - "--all mode resolves the merge-base explicitly (git rev-parse then git merge-base) rather than relying on git diff's own three-dot resolution, so orphan-history and unresolvable-base failures are caught and fail-safe BEFORE any git diff call runs"
  - "used `# shellcheck disable=SC1091` (existing plan-adversary.sh/qa-coverage-adversary.sh convention) instead of `# shellcheck source=` for review-tier.sh's source line, so `shellcheck scripts/gsd/review-tier.sh` alone is clean without needing to pair it with security-surface.sh on the same invocation"

patterns-established:
  - "Any future bash lever needing the security-surface keyword list sources scripts/gsd/security-surface.sh — do not re-inline the KEYWORDS literal"

requirements-completed: [REQ-04, REQ-05]

coverage:
  - id: D1
    description: "security-surface KEYWORDS pattern extracted to one sourceable home; security-model-fence.sh rewired to source it with zero behavior change"
    requirement: "REQ-04"
    verification:
      - kind: unit
        ref: "tests/bats/security-model-fence.bats (all 6 tests, unmodified)"
        status: pass
      - kind: unit
        ref: "tests/bats/review-tier.bats#exact-pattern pin: sourced KEYWORDS matches the verbatim expected literal"
        status: pass
    human_judgment: false
  - id: D2
    description: "review-tier.sh classifies a diff as light|standard|full via file-count/line-count/security-surface-path/migration-path rules, mirroring review-gate's --staged/--all/--file diff targets"
    requirement: "REQ-04"
    verification:
      - kind: unit
        ref: "tests/bats/review-tier.bats (24 of 27 tests covering the tier matrix, boundaries, diff-mode parity, --file mode)"
        status: pass
    human_judgment: false
  - id: D3
    description: "REVIEW_TIER={light|standard|full} env override honored with reason 'override'; invalid values ignored"
    requirement: "REQ-05"
    verification:
      - kind: unit
        ref: "tests/bats/review-tier.bats#REVIEW_TIER=light/standard/full override tests + REVIEW_TIER=garbage ignored test"
        status: pass
    human_judgment: false
  - id: D4
    description: "fail-safe posture: unresolvable base, orphan/no-merge-base history, option-injection base string, and failed git metadata calls all classify standard (never light), with warnings isolated to stderr"
    requirement: "REQ-04"
    verification:
      - kind: unit
        ref: "tests/bats/review-tier.bats#unresolvable base / orphan history / option-injection / metadata-command failure / stdout-stderr separation tests"
        status: pass
    human_judgment: false

duration: ~15min
completed: 2026-07-10
status: complete
---

# Phase 01 Plan 02: security-surface extraction + review-tier.sh diff risk detector Summary

**Extracted the shared security-surface KEYWORDS pattern into `scripts/gsd/security-surface.sh` and built `scripts/gsd/review-tier.sh`, a bash diff risk-tier detector (light/standard/full) with boundary-safe path matching, option-injection-safe base resolution, and an always-standard-never-light fail-safe posture.**

## Performance

- **Duration:** ~15 min
- **Tasks:** 2 (Task 1 refactor; Task 2 RED→GREEN TDD)
- **Files modified:** 4 (2 new, 1 new test file, 1 modified)

## Accomplishments
- `security-surface.sh` is now the single home for the security-surface keyword pattern (AC-004 "one home"), sourced by both `security-model-fence.sh` (content-grep mode, unchanged behavior) and `review-tier.sh` (path-match mode, new).
- `review-tier.sh` classifies the same diff review-gate reviews (`--staged` default with fallback to `git diff HEAD`, `--all` via `REVIEW_TIER_BASE...HEAD`, `--file <path>`) into `light|standard|full` + a reason, printed as a single stdout line with all diagnostics isolated to stderr.
- `REVIEW_TIER={light|standard|full}` env override implemented and tested; any other value falls through to auto-detection.
- Full fail-safe coverage: unresolvable base, orphan history (no merge-base), a hostile option-injection base string, and a failed `git diff --name-only`/`--numstat` call all classify `standard`, never `light`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Extract security-surface.sh + rewire fence** - `ec17e26` (refactor)
2. **Task 2: RED→GREEN — review-tier.sh tier matrix, override, fail-safe** - `52e6765` (test, RED) / `ddf57b6` (feat, GREEN)

_TDD task produced 2 commits (test → feat); no refactor commit needed._

## Files Created/Modified
- `scripts/gsd/security-surface.sh` - new sourceable lib exporting `KEYWORDS` (verbatim, moved from the fence)
- `scripts/gsd/security-model-fence.sh` - line 32 now sources `security-surface.sh`; only that line changed
- `scripts/gsd/review-tier.sh` - new diff risk-tier detector (light/standard/full)
- `tests/bats/review-tier.bats` - 27 tests: tier matrix, boundary pins, override, fail-safe, mode parity, usage errors, exact-pattern pin

## Decisions Made
- Chose `# shellcheck disable=SC1091` (matching the existing `plan-adversary.sh`/`qa-coverage-adversary.sh` convention) over `# shellcheck source=` for `review-tier.sh`'s source line, so `shellcheck scripts/gsd/review-tier.sh` run standalone (as the task's own `<verify>` command specifies) is clean without pairing it with `security-surface.sh` in the same invocation. `security-model-fence.sh` (Task 1) kept the plan-mandated `# shellcheck source=` directive as explicitly instructed in that task's `<action>`.
- `--all` mode resolves the merge-base explicitly via a separate `git merge-base` call after `git rev-parse` rather than relying solely on git's own three-dot diff resolution, so the orphan-history fail-safe path is deterministic and independently testable.
- Security-surface path matching tokenizes path separators (`/`, `.`, `_`, `-`) to spaces and uses `grep -Ewi` (whole-word match) against the shared `KEYWORDS` pattern — this satisfies the path-component/token-boundary requirement (finding 11) without needing GNU-only `\b` regex extensions, keeping the script portable.

## Deviations from Plan

None - plan executed exactly as written. Both tasks' acceptance criteria were met without needing Rule 1-4 auto-fixes.

## Issues Encountered
- Initial ad-hoc smoke-testing (before writing the bats suite) used `seq -w` for zero-padded filenames; a `seq -w 1 5` vs `seq -w 1 30` width mismatch briefly looked like a review-tier.sh bug (untracked files not appearing in `git diff`) but was a test-harness artifact, not a script defect — resolved by using explicit `printf '%02d'` padding in the bats fixtures.
- First bats run of the 4 fail-safe tests (unresolvable base / orphan / option-injection / metadata failure) failed because `run` without `--separate-stderr` merges stdout+stderr into `$output`, so the stderr WARN line landed before the stdout tier line in the combined string. Fixed by using `run --separate-stderr` for those 4 tests (plus the existing stdout/stderr-separation test), which also more precisely tests finding 9 (stdout-only tier line).

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- `review-tier.sh` and its API contract (mode flags, `REVIEW_TIER`/`REVIEW_TIER_BASE` env vars, stdout/stderr contract, exit codes) are ready for Phase 2 to wire into `skills/review-gate/SKILL.md`'s tier-selection preamble (AC-004/AC-005) — that wiring is explicitly out of scope for this plan.
- `security-surface.sh` is available for any other lever needing the security-surface keyword list; no other plan should re-inline the `KEYWORDS` literal.
- No blockers.

---
*Phase: 01-levers*
*Completed: 2026-07-10*

## Self-Check: PASSED

All created files found on disk (security-surface.sh, review-tier.sh, review-tier.bats, this SUMMARY). All 4 commit hashes (ec17e26, 52e6765, ddf57b6, 9211ebe) verified present in `git log --oneline --all`.
