---
phase: 02-skill-wiring
plan: 01
subsystem: testing
tags: [bats, review-gate, review-tier, findings-queue, gates.py, adversary-host]

requires:
  - phase: 01-levers
    provides: "scripts/gsd/review-tier.sh (tier detector), scripts/gsd/adversary-host.sh (cross-model adversary lib), lib/gates.py findings-queue (persistent findings store) — all frozen, consumed not modified"
provides:
  - "review-gate SKILL.md tier-selection preamble: mode-mirrored review-tier.sh call, REVIEW_TIER_BASE=main pinned on --all, imperative SKIP-Pass gating, FULL-tier adversary-host.sh invocation, fail-safe to standard"
  - "review-gate SKILL.md findings-queue recording: capability-probed GATES_PY, resolved-sig consult before Pass 1, findings-queue add after Merge-and-rank, resolve lifecycle, DEGRADED_PERSISTENCE footer"
  - "fixed --file arg parsing (was silently producing an empty diff and bypassing review)"
  - "tests/bats/int-review-gate.bats — 18 falsifiable grep + section-order pins (INT-001, INT-004)"
affects: [phase-03-docs-install-ship]

tech-stack:
  added: []
  patterns: ["capability-probe over first-exists for GATES_PY resolution", "cd \"$REPO_ROOT\" subshell for every gates.py call so a relative store resolves once", "grep -n line-number comparison for section-order test pins"]

key-files:
  created:
    - tests/bats/int-review-gate.bats
  modified:
    - skills/review-gate/SKILL.md

key-decisions:
  - "GATES_PY capability probe relies on gates.py returning a nonzero exit for an unsupported/unknown subcommand (verified rc=2 at lib/gates.py:1115 today) — if a future gates.py version changes unknown-command behavior to exit 0, the probe silently misidentifies a non-supporting candidate as capable. This is a documented fragility of the probe design, not a bug introduced by this plan."
  - "REQ-04 and REQ-05 were already checked [x] in .planning/REQUIREMENTS.md before this wiring existed (the levers existed but were never called from review-gate). This plan is what actually completes them — left checked per plan instruction, correction recorded here for honest traceability rather than un-checking and re-checking."
  - "Moved the --dry-run early-exit out of '### Capture diff' and into the end of the new '### Tier selection' section so the dry-run echo can name the tier (previously dry-run exited before any tier concept existed)."

requirements-completed: [REQ-04, REQ-05, REQ-07]

coverage:
  - id: D1
    description: "review-gate SKILL.md calls review-tier.sh mode-mirrored to DIFF_TARGET with REVIEW_TIER_BASE=main pinned on --all, and imperatively SKIPs Pass 2/3 at LIGHT"
    requirement: "REQ-04"
    verification:
      - kind: unit
        ref: "tests/bats/int-review-gate.bats#INT-001 (10 tests)"
        status: pass
    human_judgment: false
  - id: D2
    description: "Tier + reason print in the gate header; honest-verifier pass is never tier-gated"
    requirement: "REQ-05"
    verification:
      - kind: unit
        ref: "tests/bats/int-review-gate.bats#INT-001 (header + verifier-not-gated pins)"
        status: pass
    human_judgment: false
  - id: D3
    description: "Findings are capability-probed, recorded via findings-queue add, resolved-sig deduped, and resolved on fix — degrading visibly, never blocking"
    requirement: "REQ-07"
    verification:
      - kind: unit
        ref: "tests/bats/int-review-gate.bats#INT-004 (8 tests)"
        status: pass
    human_judgment: false
  - id: D4
    description: "--file arg parsing fixed (was silently discarding the path and producing an empty diff)"
    verification:
      - kind: unit
        ref: "tests/bats/int-review-gate.bats#INT-001 (git diff HEAD -- pin)"
        status: pass
    human_judgment: false

duration: 25min
completed: 2026-07-10
status: complete
---

# Phase 2 Plan 1: review-gate SKILL.md tier + findings-queue wiring Summary

**Wired review-tier.sh (mode-mirrored, imperative SKIP-Pass gating) and lib/gates.py findings-queue (capability-probed, resolved-sig dedup) into skills/review-gate/SKILL.md, plus fixed a broken --file arg-parsing bug that silently bypassed review.**

## Performance

- **Duration:** 25 min
- **Started:** 2026-07-10T04:20:00Z
- **Completed:** 2026-07-10T04:45:00Z
- **Tasks:** 2 (both TDD, RED→GREEN)
- **Files modified:** 2

## Accomplishments
- Tier-selection preamble in review-gate SKILL.md: calls `scripts/gsd/review-tier.sh` with the mode mirroring the run's `DIFF_TARGET` (`--staged` / `REVIEW_TIER_BASE=main scripts/gsd/review-tier.sh --all` / `--file`), fail-safes to `standard` on any lever failure or malformed token, and imperatively scopes DEFECT passes: LIGHT runs Pass 1 ONLY (literal `SKIP Pass 2`/`SKIP Pass 3`), FULL adds a mandatory refute-or-promote plus one extra `adversary-host.sh` cross-model adversary feeding the same merge-and-rank. Honest-verifier explicitly documented as never tier-gated.
- Fixed the `--file` arg-parsing bug: the old `--file*` case retained the flag itself as `DIFF_TARGET` and discarded the path, so `git diff --file...HEAD` silently produced an empty diff and bypassed review entirely. Now parses both `--file <path>` (two tokens) and `--file=<path>`, diffs via `git diff HEAD -- "$FILE_PATH"`.
- Findings-queue recording: GATES_PY resolved via a capability probe (`findings-queue list >/dev/null 2>&1` per candidate, not first-exists — skips an installed `~/.claude` copy lacking findings-queue support), resolved-sig skip-list built from the FULL `findings-queue list` before Pass 1, every merged finding recorded via `findings-queue add` after Merge-and-rank with resolved-sig matches dropped from ranking (`deduped: N` = resolved-skips only), resolve lifecycle wired for post-fix-round re-runs. Every gates.py call runs from a `cd "$REPO_ROOT"` subshell. Best-effort throughout: any nonzero exit warns + sets `DEGRADED_PERSISTENCE=1` (surfaced in the footer) — never blocks the verdict.
- Tier-aware operator output: dry-run names the tier and passes that would run, header box gained a `Tier: <tier> (<reason>)` line, cost/time line now reads tier-relative.
- `version:` bumped 1.3.0 → 1.4.0.
- `tests/bats/int-review-gate.bats` (new, 18 tests): falsifiable exact-command-line pins (capability-probe line, `REVIEW_TIER_BASE=main ... --all` call, `findings-queue resolve` call, fixed `--file` diff line) plus `grep -n` section-order comparisons (tier selection before Pass 1, findings-queue consult before Pass 1, recording between Merge-and-rank and Output-and-exit).

## Task Commits

Each task was committed atomically (TDD RED then GREEN combined into the two commits below since both tasks share the same bats file and were verified RED together before either SKILL.md edit):

1. **RED — falling bats pins for both tasks** - `5a6a4dd` (test)
2. **GREEN — tier preamble + fixed --file parsing + findings-queue recording** - `b01c03c` (feat)

**Plan metadata:** pending (this commit)

## Files Created/Modified
- `tests/bats/int-review-gate.bats` - 18 falsifiable grep + section-order pins for INT-001 (tier wiring) and INT-004 (findings recording)
- `skills/review-gate/SKILL.md` - tier-selection preamble, fixed --file parsing, findings-queue recording, tier-aware operator output, version 1.4.0

## Decisions Made
- Combined Task 1 and Task 2's RED phases into a single bats file / single RED commit (`5a6a4dd`) since the plan's own `<action>` blocks build the same file incrementally and both tasks' pins were confirmed RED together before any SKILL.md edit — the GREEN commit (`b01c03c`) then satisfies both tasks' behavior bullets in one pass since the SKILL.md sections (tier selection, findings consult, findings recording) are interleaved by design (consult must land inside the pre-Pass-1 region alongside tier selection).
- Moved the `--dry-run` early exit out of `### Capture diff` into the end of the new `### Tier selection` section so the dry-run echo can report the tier (a dry-run before tier is known can't name it) — in-scope prose restructuring, not a lever change.

## Deviations from Plan

None — plan executed exactly as written. The two tasks were combined into one RED commit + one GREEN commit rather than two separate RED/GREEN pairs because both tasks edit the same file region incrementally and the plan's own read_first/action ordering already interleaves them (findings-queue consult must sit inside the tier/pre-Pass-1 region). This is a sequencing choice, not a scope or behavior deviation — all 18 pins from both tasks pass and both tasks' `<done>` criteria are met.

## Requirements Traceability Correction

REQ-04 and REQ-05 were already checked `[x]` in `.planning/REQUIREMENTS.md` before this wiring existed (Phase 1 built the levers but nothing called them from review-gate). This plan is what actually completes them. Per the plan's explicit instruction, they are left checked — this note is the honest traceability record of the correction.

## Known Fragility (documented, not a defect)

The `GATES_PY` capability probe (`python3 "$candidate" findings-queue list >/dev/null 2>&1`) relies on `lib/gates.py`'s current behavior of returning a nonzero exit code (verified `rc=2`, `lib/gates.py:1114-1115`, "unknown command") for a `gates.py` copy that does not support the `findings-queue` subcommand. If a future `gates.py` version changes unknown-command handling to exit 0, the probe would silently misidentify a non-supporting candidate as capable and findings recording would fail downstream instead of falling through to the next candidate or degrading cleanly. This is a property of the probe design consuming `gates.py`'s existing (frozen, not modified by this plan) exit-code contract — flagged here for awareness, not fixed, since `lib/gates.py` is out of scope for this plan.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- review-gate's two Phase-1 levers (review-tier.sh, findings-queue) are now fully wired and bats-pinned; REQ-04/REQ-05/REQ-07 satisfied.
- No lever, no gates.py, and no other SKILL.md was touched — the `--file` fix is the only in-scope gate-source change beyond wiring, as required.
- Remaining Phase 2 wiring (feature-implement/learnings-harvest.sh, adopt-wip/liveness-check.sh, preflight/harness-audit.py, code-uplift/--slop-only, fix/gsd-debug) is out of scope for this plan and tracked by ROADMAP Phase 2 criterion 2's other conjuncts.

---
*Phase: 02-skill-wiring*
*Completed: 2026-07-10*

## Self-Check: PASSED
- FOUND: tests/bats/int-review-gate.bats
- FOUND: skills/review-gate/SKILL.md
- FOUND: 5a6a4dd (test: RED bats pins)
- FOUND: b01c03c (feat: tier + findings-queue wiring GREEN)
