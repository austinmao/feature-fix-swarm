# Project: feature-fix-swarm — spec 003 orchestration hardening

## What

Eight additive enhancements to FFS orchestration: delegation-enforcer hook
(auto-pin unpinned spawns), learnings harvest in finish tails, risk-sized
review-gate tiers, persistent findings queue (work ALL findings, dedup
re-runs), harness readiness audit in preflight, `--slop-only` deslop fast path,
`/fix`→gsd-debug routing, AND-of-signals liveness guardrail for autonomous
runs. Source spec: `specs/003-orchestration-hardening/spec.md` (AC-001..013);
prior art: `specs/003-orchestration-hardening/prior-art.md` (PORT decision —
oh-my-claudecode, ruflo, agent-orchestrator patterns, FFS-native builds).

## Why

FFS v4.4.0 detects orchestration failures post-hoc, prices every review the
same, loses run learnings, and kills overnight runs on single failed probes.
Prevention-first + cost-sized + memory-retaining + resilient.

## Constraints

- All work on branch `003-orchestration-hardening`; scoped `git add <files>`
  only; NEVER push/merge without a recorded grant.
- Never touch `gates.py verify_done`/`run_gate` exit-code semantics
  (findings-queue is a NEW subcommand + store key, additive only).
- New bash levers → `scripts/gsd/` (hook → `scripts/hooks/`); shellcheck-clean;
  bash 3.2-safe (macOS default): no mapfile, no `read -a`. Python → pytest.
- Fail-open/advisory posture for enforcer + harness-audit + learnings (never
  block a run); fail-safe `standard` for review-tier on doubt.
- Suite baselines (must stay ≥, 0 failures): pytest 231, bats 64.

## Testing Policy (testing-policy skill)

- Mock-minimization ladder: real script under test always; stub only external
  bins (gbrain, gh) via PATH shims at the process boundary; never mock the
  lever being tested.
- RED-first mandatory: every behavior gets a failing test before implementation.
- Coverage floor 80% on new python; bats happy+error path per bash lever.
- No browser surface in this repo — canary/browser doctrine N/A (plan.md Test
  Strategy note).
