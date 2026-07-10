---
phase: 03-docs-install-ship
plan: 01
subsystem: installer, docs
tags: [setup.sh, bats, changelog, readme, INT-003]

requires:
  - phase: 01-levers
    provides: "scripts/hooks/delegation-enforcer.sh, scripts/gsd/security-surface.sh, scripts/gsd/review-tier.sh, scripts/gsd/liveness-check.sh, scripts/gsd/learnings-harvest.sh, scripts/harness-audit.py — all frozen, consumed not modified"
  - phase: 02-skill-wiring
    provides: "review-gate/adopt-wip/preflight/fix/code-uplift skill wiring to the six levers above"
provides:
  - "setup.sh installs all six v4.5.0 levers into consumer repos (delegation-enforcer.sh via hook loop, 4 gsd scripts via gsd_script loop, harness-audit.py via script loop) — closes INT-003"
  - "tests/bats/setup-install.bats — 3 new manifest-assert cases pinning the six levers"
  - "CHANGELOG.md v4.5.0 entry — 8 enhancements, cites spec 003"
  - "README.md 'What's in the box' scripts/ count corrected to current reality (22, was stale at 8)"
affects: []

tech-stack:
  added: []
  patterns: ["manifest-assert bats convention: grep the '^for X in ' line, substring-match each expected entry"]

key-files:
  created: []
  modified:
    - setup.sh
    - CHANGELOG.md
    - README.md
    - tests/bats/setup-install.bats

key-decisions:
  - "CHANGELOG's 8-enhancement count groups the phase-1/phase-2 deliverables (9 SUMMARY.md files) into 8 bullets by combining security-surface.sh+review-tier.sh (built together in 01-02) into one bullet, and folding the setup.sh-manifest-extension work (this plan) into the entry's intro sentence rather than a 9th bullet — the plan's own INT-003 closure is infrastructure for the release, not a user-facing enhancement in the same sense as the other 8."
  - "README scripts/ tree entry rewritten (not just the count bumped): the prior text listed a nonexistent 'harness/' subdir and omitted scripts/gsd/ entirely (16 lever scripts, the majority of the directory) — corrected to name gsd/ and hooks/ and drop the stale harness/ reference, per the plan's 'count, don't guess' instruction."
  - "skills count (16) and bats-test-count claims elsewhere in README were already accurate on inspection — left unchanged (no speculative rewrite of correct text)."

requirements-completed: [REQ-12, REQ-13]

coverage:
  - id: D1
    description: "setup.sh installs all six new levers (delegation-enforcer.sh, security-surface.sh, review-tier.sh, liveness-check.sh, learnings-harvest.sh, harness-audit.py)"
    requirement: "REQ-12"
    verification:
      - kind: unit
        ref: "tests/bats/setup-install.bats (3 new cases: gsd-script manifest, hook manifest, script manifest)"
        status: pass
    human_judgment: false
  - id: D2
    description: "CHANGELOG.md v4.5.0 entry present, cites spec 003, 8 enhancements"
    requirement: "REQ-13"
    verification:
      - kind: unit
        ref: "grep -q '4.5.0' CHANGELOG.md; bullet count == 8"
        status: pass
    human_judgment: false

duration: 20min
completed: 2026-07-10
status: complete
---

# Phase 3 Plan 1: setup.sh installer manifest + CHANGELOG v4.5.0 + README counts Summary

**setup.sh now installs all six v4.5.0 orchestration-hardening levers into consumer repos (INT-003), extending `tests/bats/setup-install.bats` RED-first; CHANGELOG.md gained the v4.5.0 entry (8 enhancements, cites spec 003) and README's stale scripts/ count was corrected by counting rather than guessing.**

## Performance
- **Duration:** ~20 min
- **Tasks:** 2 (RED, GREEN)
- **Files modified:** 4

## Accomplishments
- `tests/bats/setup-install.bats` — 3 new manifest-assert cases: gsd-script manifest includes `security-surface.sh`/`review-tier.sh`/`liveness-check.sh`/`learnings-harvest.sh`; hook manifest includes `delegation-enforcer.sh`; script manifest includes `harness-audit.py`. Confirmed RED (all 3 failing) before touching setup.sh.
- `setup.sh` — extended 3 existing install loops: the `for gsd_script in ...` loop gained `gsd/security-surface.sh gsd/review-tier.sh gsd/liveness-check.sh gsd/learnings-harvest.sh`; the `for hook in ...` loop gained `delegation-enforcer.sh`; the `for script in ...` loop gained `harness-audit.py`. No new loops, no new logic — the six levers now ride the same battle-tested install/self-copy-guard/chmod path every other lever uses.
- `CHANGELOG.md` — new `## v4.5.0` entry above v4.4.0, 8 bullets (one per phase-1/phase-2 deliverable, security-surface.sh+review-tier.sh combined since they were built together), cites `specs/003-orchestration-hardening/`.
- `README.md` — "What's in the box" `scripts/` line corrected from a stale `8 bash scripts` (which listed a nonexistent `harness/` subdir and omitted `scripts/gsd/` — 16 scripts, the majority of the directory) to `22 scripts (bash + harness-audit.py)`, naming `gsd/` and `hooks/` (with delegation-enforcer.sh called out).
- Verified counts by counting, not guessing: `find scripts -type f -name '*.sh' | wc -l` → 22 total, `find tests/bats -name '*.bats' | wc -l` → 14 files / 143 `@test` cases, `find skills -maxdepth 1 -mindepth 1 -type d | wc -l` → 16 (already correct in README, left unchanged).

## Task Commits
1. **Plan metadata** - `b221037` (plan)
2. **RED — 3 falling manifest-assert cases** - `89c773d` (test)
3. **GREEN — setup.sh manifest extension** - `05f0340` (feat)
4. **CHANGELOG v4.5.0 + README counts** - `7920532` (docs)

## Files Created/Modified
- `tests/bats/setup-install.bats` - 3 new `@test` cases (gsd-script, hook, script manifest assertions for the 6 levers)
- `setup.sh` - 3 install-loop lines extended (no new logic)
- `CHANGELOG.md` - new `## v4.5.0` entry, 8 bullets, cites spec 003
- `README.md` - `scripts/` tree entry corrected (count + `gsd/`/`hooks/` naming)

## Deviations from Plan
None — plan executed exactly as written. RED confirmed before GREEN; verification thresholds (bats ≥140, pytest ≥250, `grep -q '4.5.0'`) all met.

## Verification Results
- `bats tests/bats/setup-install.bats` — 5/5 pass (2 pre-existing + 3 new)
- `bats tests/bats/` — **143/143 pass** (exit 0; required ≥140)
- `python3 -m pytest -q` — **250 passed, 0 failed** (required ≥250)
- `grep -q '4.5.0' CHANGELOG.md` — match found

## Issues Encountered
None.

## User Setup Required
None.

## Next Phase Readiness
INT-003 (setup.sh-install pin) is closed. Ship tail (review-gate → PR) is explicitly out of scope for this plan per the objective — runs in the main loop under recorded grants.

---
*Phase: 03-docs-install-ship*
*Completed: 2026-07-10*

## Self-Check: PASSED
- FOUND: setup.sh
- FOUND: CHANGELOG.md
- FOUND: README.md
- FOUND: tests/bats/setup-install.bats
- FOUND: b221037 (plan metadata)
- FOUND: 89c773d (test: RED)
- FOUND: 05f0340 (feat: GREEN)
- FOUND: 7920532 (docs: CHANGELOG + README)
