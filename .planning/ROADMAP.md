# Roadmap: GSD adoption support tooling

## Overview

One phase: build two small, independent, shellcheck-clean support scripts with
tests, TDD, in parallel where possible.

## Phases

- [ ] **Phase 1: Support scripts** - consent-check.sh + state-phase.sh with tests

## Phase Details

### Phase 1: Support scripts
**Goal**: Deterministic assertions over gsd-side state for FFS preflight and gates
**Depends on**: Nothing (first phase)
**Requirements**: REQ-01, REQ-02
**Success Criteria** (what must be TRUE):
  1. `bash scripts/gsd/consent-check.sh ffs-gates` exits 1 (not installed yet) with an actionable message
  2. `bash scripts/gsd/state-phase.sh .planning/STATE.md` prints an integer and exits 0
  3. Both scripts shellcheck-clean; their tests pass; `python3 -m pytest lib/tests -q` still 190 passed
**Plans**: 2 plans

Plans:
- [ ] 01-01: consent-check.sh + test (REQ-01)
- [ ] 01-02: state-phase.sh + test (REQ-02)
