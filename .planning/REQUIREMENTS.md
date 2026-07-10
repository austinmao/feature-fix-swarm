# Requirements: spec 003 orchestration hardening

One REQ per acceptance criterion in `specs/003-orchestration-hardening/spec.md`
(AC text is authoritative; REQs restate testably).

## v1 Requirements

### Delegation enforcer (US1)

- [ ] **REQ-01**: `scripts/hooks/delegation-enforcer.sh` reads PreToolUse JSON on stdin; Agent/Task input without `model` → same JSON with `model` injected from `.planning/config.json` resolution; with `model` present → byte-identical passthrough; no/invalid config → passthrough + stderr warn; exit 0 in all non-usage cases. [AC-001]
- [ ] **REQ-02**: `DELEGATION_ENFORCER=off` → unconditional passthrough. [AC-002]

### Learnings harvest (US2)

- [ ] **REQ-03**: `scripts/gsd/learnings-harvest.sh` reads `.planning/**/learnings*.jsonl`, writes distilled entries to gbrain when healthy, else appends atomically to `.feature-fix-swarm/learnings-archive.jsonl`; always exit 0; prints harvested count. feature-implement + code-uplift finish tails invoke it. [AC-003]

### Review tiers (US3)

- [x] **REQ-04**: `scripts/gsd/review-tier.sh` emits `light|standard|full` + reason: light = <5 files AND <200 lines AND no security-surface path; full = >20 files OR security-surface OR migration; else standard. Security patterns sourced from shared `scripts/gsd/security-surface.sh` (extracted from security-model-fence.sh, fence behavior unchanged). review-gate SKILL.md carries the tier preamble scoping its defect passes. [AC-004]
- [x] **REQ-05**: `REVIEW_TIER` env overrides detection; tier + reason printed in gate header. [AC-005]

### Findings queue (US4)

- [ ] **REQ-06**: `gates.py findings-queue add|list|resolve` with stable sha256(file+normalized-issue) signatures, stored under a `findings` key in the existing evidence store; unit-tested; unrelated store keys untouched. [AC-006]
- [ ] **REQ-07**: review-gate records ALL pass findings via findings-queue before verdict; re-runs skip resolved signatures and report `deduped: N`. [AC-007]

### Harness audit (US5)

- [ ] **REQ-08**: `scripts/harness-audit.py` scores 0-100 (dangling skill links, vendored-copy version drift, dead model pins, hook-registration drift) with `--json`; preflight skill gains an advisory section invoking it; never blocks. [AC-008]

### Deslop fast path (US6)

- [ ] **REQ-09**: `code-uplift --slop-only <diff-base>`: refuse on failing baseline suite; deletion-first, scope = diff files only; suite re-run after; net line delta reported. [AC-009]

### fix→gsd-debug (US7)

- [ ] **REQ-10**: fix SKILL.md diagnose step routes to `/gsd-debug` when no failing test AND no single-command repro exists. [AC-010]

### Liveness guardrail (US8)

- [ ] **REQ-11**: `scripts/gsd/liveness-check.sh`: ALIVE iff (pid alive) OR (run-state mtime within `LIVENESS_WINDOW_MIN`, default 30) OR (granted unconsumed ship action in flight); exit 0 alive / 1 dead; per-signal verdicts printed; adopt-wip consults it before abandonment. [AC-011]

### Quality + docs (cross-cutting)

- [ ] **REQ-12**: shellcheck-clean bash, pytest-covered python, bats happy+error per new script; suites ≥ baseline (pytest 231, bats 64), 0 failures. [AC-012]
- [ ] **REQ-13**: CHANGELOG v4.5.0, README counts, setup.sh installs everything (asserted in setup-install.bats). [AC-013]

## Out of Scope

Tournament selection, CDC/SSE bus, multi-CLI adapters, ETag polling, persistent
reviewer sessions, any change to gates.py verify_done/run_gate exit semantics.
