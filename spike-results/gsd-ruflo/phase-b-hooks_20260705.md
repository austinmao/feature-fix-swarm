# Phase B — ffs-gates as host-side hooks (re-design, complete)

**Date:** 2026-07-05 · **Status: COMPLETE** (re-designed from capability-native per research)

## Why re-designed

Third-party command-based blocking capability gates are non-functional at gsd-core 1.6.1
(upstream #2004 closed-confirming; #2009 still open post-1.7.0-rc.2). The plan's
`capabilities/ffs-gates/` bundle would install but never gate. Enforcement moved host-side:
FFS-owned Claude Code hooks re-keyed to gsd planning artifacts. Capability-native port
deferred until a gsd-core release proves third-party gate evaluation (re-check via #2009).

## Delivered

| plan gate | delivery | proof |
|---|---|---|
| `verify:post` (blocking) | `scripts/hooks/gsd-phase-evidence-gate.sh` — PreToolUse Edit\|Write; blocks `.planning/ROADMAP.md` phase-checkbox flips + `.planning/STATE.md` `completed_phases` increments without `GATES_STRICT=1 gates.py verify-done` evidence (`gsd-phase-N`, fallback `gsd-phase`) | `tests/gsd-phase-evidence-gate.bats` 7/7; shellcheck clean |
| `ship:pre` (grant ledger) | `scripts/gsd/review-gate-command.sh` (Phase A) — fail-closed REVISE + `pending` recorded without typed `ship:gsd` grant | proven live Phase A |
| `plan:post` (lineage) | deferred — openclaw-side lineage enforcement is uncommitted (operator-gated branch); hook contract is openclaw-blocking/else-skip, and in FFS it is always skip. Wire when lineage lever merges. | n/a |
| consent wall | re-mapped: no capability bundle ⇒ no gsd consent surface. Claude Code's per-machine repo-hook trust approval is the analogous wall. `scripts/gsd/consent-check.sh` (pilot REQ-01) stays ready for the capability-native port. | consent-check 7/7 bats |

Wiring: repo `.claude/settings.json` PreToolUse registration (gsd-managed `settings.local.json` untouched).

## Conformance (plan §Verification Phase B)

- force verify:post failure → **blocked exit 2** (bats: flip-without-evidence, STATE increment) ✓
- force ship without grant → **REVISE + pending recorded** ✓ (Phase A evidence)
- lineage uncited → deferred with the lever itself
- minus consent → re-mapped to Claude Code hook-trust prompt (above)

Full suites green after wiring: 24 bats, 190 pytest.
