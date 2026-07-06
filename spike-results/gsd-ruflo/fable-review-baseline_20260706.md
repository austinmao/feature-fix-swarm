# Fable review baseline — FFS gsd integration (2026-07-06)

Reviewer: Fable 5 (main session), post spec-256 live conformance (Phase 1 PASS,
opus-verified). Scope: `scripts/gsd/*`, `scripts/hooks/gsd-phase-evidence-gate.sh`,
gsd-native skills, `templates/gsd-config.base.json`, plus run-observed behavior.

## Findings (fixed in this pass)

| Sev | Where | Defect | Fix |
|---|---|---|---|
| HIGH | `review-gate-command.sh` | `RUN_ID="${GSD_RUN_ID:-spec-002}"` — every non-002 ship checked grants under spec-002's ledger key | env > branch-derived `spec-NNN` > fail-closed REVISE; feature-implement now exports `GSD_RUN_ID` |
| HIGH | `gates-test-command.sh` | Evidence laundering: repos without `lib/tests` fell back to `bash -n <self>` — a vacuous syntax check recorded runner-executed verify-done evidence that legally unlocked ROADMAP phase flips. spec-256's machine evidence was exactly this; real verification lived only in the lead/verifier ROADMAP-criteria re-runs | `GSD_TEST_CMD` env > `.planning/gsd-test-command` file > `lib/tests`; no test command → exit 1 (refuse vacuous evidence); spec-decompose seeds the file |
| HIGH (cost) | `gsd-run.sh` | Lead model unpinned → session default (opus). Lead is mechanical orchestration; opus lead = $17.73 of the $51.54 spec-256 run for zero quality gain | `GSD_LEAD_MODEL` env, default `claude-sonnet-5` |
| MEDIUM | `review-gate-command.sh` | codex exec failure/absence → APPROVED (fail-open ship review). Deliberate fail-soft; acceptable while ship also sits behind the grant ledger, but note it in reports | flagged, not changed |
| MEDIUM | `gsd-phase-evidence-gate.sh` | Generic `gsd-phase` evidence id unlocks LATER phase flips (prior phase's evidence reused) | already ponytail-annotated; per-phase ids close it when it bites |
| LOW | `mempalace` shim | `mine` puts artifact verbatim — unbounded size into gbrain | accept until it hurts |
| LOW | `state-phase.sh` | `Status:` "phase complete" substring heuristic — fixture-tested against real templates, but upstream STATE.md phrasing is a moving target (ADR-1769) | re-verify on gsd-core upgrade |

## Run-observed lessons (spec-256 conformance, $51.54 for plan+execute phase 1)

1. **Model split**: sonnet $22.92 / opus $17.73 / fable $10.87 / haiku $0.
   Cache-read dominated (sonnet 43M, opus 6.3M, fable 3.2M tokens). The lead's
   200k-context re-read of the 1771-line workflow each drive is the cost core —
   sonnet lead default recovers ~⅓ of run cost.
2. **haiku never triggered**: dynamic light tier exists but phase-1 tasks were
   all standard/heavy. If light-tier usage matters, plan granularity must go
   finer — do not expect haiku traffic on coarse plans.
3. **Verification chain is real, but the machine evidence layer was the weak
   link** (finding 2). The strong links were: lead independent gate re-run +
   opus verifier independent re-run + human_needed escalation on
   grep-can't-prove-absence claims.
4. **Aborted headless drives cost money invisibly** — plan-phase 1/1b logs have
   no result events (2 aborted attempts before 1c's $21.03). Watchers must kill
   or confirm fast; every abort re-pays the workflow read.
5. **Wrong-parser trap**: gsd spawns via `Agent` tool — stream-json milestone
   grep must match `"subagent_type":"gsd-X","model":"Y"` JSON, never prose.

## Verdict

Integration is sound (walls, ladder, verifier chain all proven live); the two
HIGHs were exactly the kind of defect the machinery exists to prevent — both
were in the machinery itself, not the product code. Fixed + tested this pass.
