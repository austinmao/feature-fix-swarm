# Phase A Pilot Verdict — gsd-core 1.6.1 as FFS orchestrator

**Date:** 2026-07-05
**Branch:** `002-gsd-replaces-ruflo` (local only)
**Verdict: PASS** — proceed to Phase B (ffs-gates capability via FFS-owned hooks).

## What ran

Full gsd loop on a real 2-requirement pilot (`consent-check.sh` + `state-phase.sh`),
headless via `scripts/gsd/gsd-run.sh` (trimmed-MCP, auth-scrubbed):

1. `/gsd-plan-phase 1` — research (539-line report, HIGH confidence) → 2 wave-1 plans → plan-checker pass
2. `/gsd-execute-phase 1` — 2 concurrent executors, TDD RED→GREEN, autonomous post-merge test gate, opus verifier
3. Verifier **BLOCKED** phase completion (2/3 must-haves): caught `state-phase.sh` parsing a fabricated
   STATE.md fixture format, not the real gsd-core template — its 8 passing bats tests were false confidence
4. `/gsd-plan-phase 1 --gaps` → `/gsd-execute-phase 1 --gaps-only` — gap plan, real-template fixtures (RED
   `754d01b`), fix (`139a9b1`), fresh verifier re-run: **3/3 passed**, milestone complete

Live proof post-run: `bash scripts/gsd/state-phase.sh .planning/STATE.md` → prints integer, exit 0.
`python3 -m pytest lib/tests -q` → 190 passed throughout. consent-check 7/7, state-phase 10/10, shellcheck clean.

## Gate scoreboard (plan Phase A step 5)

| # | Gate | Result | Evidence |
|---|---|---|---|
| i | gates.py fires as gsd test_command; failure blocks | **PROVEN** | `gsd-phase` run_gate entry in `.feature-fix-swarm/evidence.json` (exit 0), fired autonomously by the drive at 22:48; block-half proven at wrapper level + verifier refused `phase.complete` on real defect |
| ii | ≥2 concurrent executors, no corruption | **PROVEN** | 01-01/01-02 commits interleave 22:37–22:47; worktree merge clean |
| iii | dynamic-routing escalation event | **PARTIAL (intent proven)** | No literal `dynamic_routing` escalation fired — nothing failed at the routing layer. The failure-recovery loop the gate exists to test DID run end-to-end: verifier block → `--gaps` plan → `--gaps-only` execute → re-verify green. Literal event = untested-optional. |
| iv | autonomy-grant wall on ship | **PROVEN** | `review-gate-command.sh` fail-closed REVISE + pending recorded without grant; APPROVED path with typed `ship:gsd` grant |
| v | zero context overflows | **PROVEN** | 0 overflow matches across all drive logs under trimmed-MCP runner |

## Strongest signal

The verifier catching a genuine defect (fabricated fixtures masking a real parser bug), refusing completion,
and the gap loop fixing it with proper TDD — unattended — is exactly the adoption thesis: gsd orchestrates,
deterministic gates + adversarial verify hold the line. gates.py stayed sole completion authority throughout.

## Known traps (carried into Phase B+)

- Bare `npx gsd` = wrong package; use `node node_modules/.bin/gsd-tools`
- `GATES_STRICT` must not prefix `run-gate` (leaks into child pytest env)
- codex echoes prompt → line-anchor verdict parse (`^VERDICT: (PASS|BLOCK)$`, tail -1)
- Shell `ANTHROPIC_API_KEY` 401s headless drives — scrub in runner
- STATE.md frontmatter counters unreliable (upstream bug class, re-confirmed) — `state-phase.sh` derives from body
- Third-party capability gates non-functional at 1.6.1 (#2004; #2009 open post-1.7.0-rc.2) → Phase B uses
  FFS-owned Claude Code hooks re-keyed to gsd artifacts; capability-native deferred
- TDD checker advisory: greps `feat(NNN):` for GREEN; `fix(NNN):` green commits flag falsely (cosmetic)
