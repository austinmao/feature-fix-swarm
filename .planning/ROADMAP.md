# Roadmap: GSD adoption support tooling

## Overview

One phase: build two small, independent, shellcheck-clean support scripts with
tests, TDD, in parallel where possible.

## Phases

- [x] **Phase 1: Support scripts** - consent-check.sh + state-phase.sh with tests (completed 2026-07-05)

## Phase Details

### Phase 1: Support scripts

**Goal**: Deterministic assertions over gsd-side state for FFS preflight and gates
**Depends on**: Nothing (first phase)
**Requirements**: REQ-01, REQ-02
**Success Criteria** (what must be TRUE):

  1. `bash scripts/gsd/consent-check.sh ffs-gates` exits 1 (not installed yet) with an actionable message
  2. `bash scripts/gsd/state-phase.sh .planning/STATE.md` prints an integer and exits 0
  3. Both scripts shellcheck-clean; their tests pass; `python3 -m pytest lib/tests -q` still 190 passed

**Plans**: 2/2 plans complete

Plans:

- [x] 01-01-PLAN.md
- [x] 01-02-PLAN.md
- [x] 01-01: consent-check.sh + test (REQ-01)
- [x] 01-02: state-phase.sh + test (REQ-02)
