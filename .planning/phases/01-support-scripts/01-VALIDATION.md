---
phase: 1
slug: support-scripts
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-05
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | bats-core 1.13.0 (shell) + shellcheck; pytest 9.0.2 (existing Python regression baseline) |
| **Config file** | none — bats needs no config; no pytest config exists |
| **Quick run command** | `shellcheck -S warning scripts/gsd/consent-check.sh scripts/gsd/state-phase.sh && bats tests/consent-check.bats tests/state-phase.bats` |
| **Full suite command** | `python3 -m pytest lib/tests -q` (must remain "190 passed") + `bats tests/*.bats` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `shellcheck -S warning scripts/gsd/<file>.sh && bats tests/<file>.bats`
- **After every plan wave:** Run `python3 -m pytest lib/tests -q` + full `bats tests/*.bats`
- **Before `/gsd-verify-work`:** Full suite must be green, plus the literal success-criterion commands from ROADMAP.md run once
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 1-01-* | 01 | 1 | REQ-01 | — | capability-id never string-interpolated into `node -e` (passed via `process.argv`) | unit (bats) | `bats tests/consent-check.bats` | ❌ W0 | ⬜ pending |
| 1-01-* | 01 | 1 | REQ-01 | — | exits 1 with actionable message when capability absent/inactive/gsd-tools missing | smoke | `bash scripts/gsd/consent-check.sh ffs-gates; echo $?` | ❌ W0 | ⬜ pending |
| 1-02-* | 02 | 1 | REQ-02 | — | derives phase from `## Current Position` body only, never frontmatter counters | unit (bats) | `bats tests/state-phase.bats` | ❌ W0 | ⬜ pending |
| all | 01+02 | 1 | REQ-01, REQ-02 | — | N/A | regression | `python3 -m pytest lib/tests -q` (190 passed) | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/consent-check.bats` — stubs for REQ-01 (hermetic: stubbed `gsd-tools`-shaped fake; `REPO_ROOT` overridable via env var mirroring `HOST_EXECUTOR_ROOT` pattern in `scripts/harness/ruflo-host-executor.bats`)
- [ ] `tests/state-phase.bats` — stubs for REQ-02 (fixture STATE.md files in `$BATS_TEST_TMPDIR`)
- [ ] Framework install: none — bats and shellcheck already present in dev and CI

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| shellcheck on `scripts/gsd/*.sh` in CI | REQ-01, REQ-02 | CI shellcheck job glob does not include `scripts/gsd/*.sh` | Run `shellcheck -S warning scripts/gsd/consent-check.sh scripts/gsd/state-phase.sh` locally; optional one-line CI glob fix at planner's discretion |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
