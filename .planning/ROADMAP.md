# Roadmap: spec 003 orchestration hardening

## Overview

Three phases: (1) six independent testable levers, wave-parallel, RED-first;
(2) skill-prose wiring that consumes them; (3) docs + installer + ship tail.

## Phases

- [ ] **Phase 1: Levers** — delegation-enforcer, security-surface + review-tier, liveness-check, learnings-harvest, harness-audit, gates.py findings-queue
- [ ] **Phase 2: Skill wiring** — review-gate tiers + findings recording, finish-tail learnings, code-uplift --slop-only, fix→gsd-debug, adopt-wip liveness, preflight harness-audit
- [ ] **Phase 3: Docs + install + ship** — setup.sh, CHANGELOG v4.5.0, README, full gate, review-gate, PR

## Phase Details

### Phase 1: Levers

**Goal**: Six shellcheck-clean/pytest-covered levers, each RED-first, no cross-deps (max wave width)
**Depends on**: Nothing (first phase)
**Requirements**: REQ-01, REQ-02, REQ-03 (lever half), REQ-04 (lever half), REQ-05, REQ-06, REQ-08 (lever half), REQ-11 (lever half)
**Success Criteria** (what must be TRUE):

  1. `bats tests/bats/delegation-enforcer.bats tests/bats/review-tier.bats tests/bats/liveness-check.bats tests/bats/learnings-harvest.bats` exits 0
  2. `python3 -m pytest lib/tests/test_findings_queue.py tests/test_harness_audit.py -q` exits 0
  3. `shellcheck scripts/gsd/review-tier.sh scripts/gsd/security-surface.sh scripts/gsd/liveness-check.sh scripts/gsd/learnings-harvest.sh scripts/hooks/delegation-enforcer.sh` exits 0
  4. `bats tests/bats/security-model-fence.bats` still exits 0 (extraction regression pin)
  5. `python3 -m pytest -q` ≥231 passed, 0 failed

### Phase 2: Skill wiring

**Goal**: Every lever consumed by its owning skill; prose contracts grep-pinned by bats
**Depends on**: Phase 1
**Requirements**: REQ-03 (wiring half), REQ-04 (preamble half), REQ-07, REQ-08 (preflight half), REQ-09, REQ-10, REQ-11 (adopt-wip half)
**Success Criteria** (what must be TRUE):

  1. `bats tests/bats/` exits 0 including new INT-001..006 grep-pin tests
  2. `grep -q 'review-tier.sh' skills/review-gate/SKILL.md && grep -q 'learnings-harvest.sh' skills/feature-implement/SKILL.md && grep -q 'liveness-check.sh' skills/adopt-wip/SKILL.md && grep -q 'harness-audit.py' skills/preflight/SKILL.md && grep -q -- '--slop-only' skills/code-uplift/SKILL.md && grep -q 'gsd-debug' skills/fix/SKILL.md` exits 0
  3. `python3 -m pytest -q` ≥231 passed, 0 failed

### Phase 3: Docs + install + ship

**Goal**: Installer + docs current; full gate green; shipped via review-gate → PR
**Depends on**: Phase 2
**Requirements**: REQ-12, REQ-13
**Success Criteria** (what must be TRUE):

  1. `bats tests/bats/setup-install.bats` exits 0 with new-script assertions added
  2. `grep -q '4.5.0' CHANGELOG.md` exits 0
  3. `python3 -m pytest -q && bats tests/bats/` — pytest ≥231, bats ≥64, 0 failures
  4. review-gate PASS recorded; PR opened (merge is grant-walled)
