# Prior art — spec 003 orchestration hardening

Researched 2026-07-10 via three parallel sonnet researcher agents (live `gh` reads
of each repo; star counts + push dates verified same day). Vindication threshold
`PRIOR_ART_MIN_STARS=200` — all three candidates far exceed it.

| candidate | type | stars/activity | applicability verdict | evidence |
|---|---|---|---|---|
| Yeachan-Heo/oh-my-claudecode (OMC) | Claude Code skill/hook suite | 37,620★, pushed 2026-07-09 | PORT patterns (delegation-enforcer, verification tiers, ai-slop-cleaner); architecture too divergent to adopt wholesale | `docs/DELEGATION-ENFORCER.md`, `docs/shared/verification-tiers.md`, `skills/ai-slop-cleaner/SKILL.md`, `skills/ralph/SKILL.md` |
| ruvnet/ruflo | MCP swarm runtime | 63,700★, pushed 2026-07-10 | PORT ideas only (auto pattern capture, MetaHarness readiness score); MCP runtime itself REJECTED — FFS removed it in v4.0.0 (spec 002), do not re-adopt | `plugins/ruflo-agentdb/README.md` (post-task hooks), `plugins/ruflo-metaharness/README.md` (ADR-150) |
| AgentWrapper/agent-orchestrator (AO) | Go daemon + desktop orchestrator | 8,160★, pushed 2026-07-09 | PORT patterns (multi-condition nudge reducer w/ per-condition dedup, AND-of-signals termination guardrail); Go daemon/CDC/SSE infra REJECTED as not lever-shaped for a skill suite | `backend/internal/lifecycle/reactions.go`, `docs/architecture.md` §Termination Guardrails + §Status Derivation |

## Decision input

- All three candidates are vindicated (stars ≫ threshold, active daily) and
  applicability-verified by researchers who opened the cited files.
- None is adoptable as a dependency: OMC and FFS are competing skill-suite
  architectures; ruflo's runtime was already removed from FFS by deliberate
  migration (spec 002 — re-adopting reverses a documented decision); AO is a
  Go daemon whose value here is its *patterns*, not its binary.
- Ideas confirmed already-covered in FFS (skip, no double-build): cross-vendor
  adversarial review (OMC `/ask`, AO reviewers), SPARC-style phase gates +
  traceability (ruflo — gsd loop + gates.py equivalent), pause/resume state
  machine (ruflo — gsd resume + /adopt-wip), derived-status-not-cached (AO —
  gates.py already recomputes from evidence store).
- Ideas explicitly deferred with reasons (out of scope §): tournament/arena
  selection (OMC self-improve + ruflo arena — 2-3× cost per task, no measured
  plateau yet), CDC/SSE bus, 23-harness adapter contract, ETag polling
  (LOW-tier micro-change), persistent reviewer sessions.

**Decision: PORT (build-fresh implementations of 8 verified patterns, FFS-native
bash/python levers + skill prose).** Nothing adoptable as-is; every ported
pattern cites its source file above so plan.md inherits the citations.
