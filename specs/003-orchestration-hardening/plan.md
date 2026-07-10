# Plan — spec 003 orchestration hardening

## Prior-art decision (required citation)

Per `specs/003-orchestration-hardening/prior-art.md`: **PORT** — build-fresh
FFS-native implementations of 8 patterns verified in oh-my-claudecode (37.6k★),
ruflo (63.7k★), agent-orchestrator (8.2k★). No candidate adoptable as a
dependency (competing architecture / deliberately-removed runtime / Go daemon).
Per-pattern source citations live in the prior-art table; each phase below
names its source pattern.

## Architecture notes

- All new bash levers live in `scripts/gsd/` (adversary-host.sh precedent)
  except delegation-enforcer, which is a *hook* → `scripts/hooks/` (new dir;
  consumer repos wire it via their own `.claude/settings.json` — FFS ships the
  script + registration snippet in docs, never writes consumer settings).
- `findings-queue` extends `lib/gates.py` (evidence store already owns
  run-scoped JSON state; a second store would violate single-authority).
- `harness-audit.py` is python (scoring + JSON output — same rationale as
  gates.py); lives in `scripts/` root because it audits the harness, not gsd.
- Security-surface pattern list: extract the regex from
  `scripts/gsd/security-model-fence.sh` into `scripts/gsd/security-surface.sh`
  (sourceable, one home) — fence + review-gate tier detection both source it.
  Tier detection itself = `scripts/gsd/review-tier.sh` (emits `light|standard|full`
  + reason line; review-gate SKILL.md calls it — logic in a testable lever, not
  prose arithmetic).
- Liveness composite: `scripts/gsd/liveness-check.sh` consumes run-state dir
  (`scripts/gsd/run_state/` convention) + optional `GATES_STORE` for in-flight
  ship grants. Truth table: ALIVE if (pid alive) OR (mtime fresh) OR (granted
  unconsumed ship action in flight); DEAD only when all three fail.
- Learnings harvest: `scripts/gsd/learnings-harvest.sh` globs
  `.planning/**/learnings*.jsonl`; backend probe = `command -v gbrain` +
  `env -u DATABASE_URL gbrain doctor` (matches memory-routing discipline);
  fallback archive append is atomic (tmp+mv).

## Test Strategy note (E2E adaptation)

This repo is a CLI skill suite — no browser surface. The feature-spec E2E
Playwright convention maps to **bats journeys** (`tests/bats/*.bats`), one per
PATH-NNN in spec.md. jsdom/MSW/testing-policy browser doctrine not applicable
here; mock-minimization still applies (stub external bins via PATH shims, never
mock the script under test).

## Unit Test List

Sequenced design-critical first:

- [ ] review-tier.sh: security-surface file in 2-file diff → `full` (the rule that must never regress)
- [ ] review-tier.sh: 3 files / 150 lines / no security → `light`
- [ ] review-tier.sh: 25 files → `full`; 8 files → `standard`
- [ ] review-tier.sh: `REVIEW_TIER=full` override wins + reason says `override`
- [ ] review-tier.sh: unresolvable diff base → `standard` (fail-safe default) + warn
- [ ] gates.py findings_add: stable signature = sha256(file + normalized issue text); same finding twice → one entry
- [ ] gates.py findings_list: `--unresolved` filters resolved
- [ ] gates.py findings_resolve: unknown signature → error, exit nonzero
- [ ] gates.py findings roundtrip preserves unrelated evidence-store keys
- [ ] delegation-enforcer: unpinned Agent JSON + config → model injected, JSON valid
- [ ] delegation-enforcer: pinned JSON → byte-identical stdout
- [ ] delegation-enforcer: no config / bad JSON / `DELEGATION_ENFORCER=off` → passthrough (+ warn where applicable)
- [ ] liveness-check: truth table all 8 combinations (3 signals) — DEAD only at FFF
- [ ] liveness-check: garbage/missing pid file → treats pid signal dead, not crash
- [ ] harness-audit: dangling symlink fixture → finding + score deduction
- [ ] harness-audit: version-drift fixture (skill 2.3.0 vs packaged 2.2.0) → finding
- [ ] harness-audit: clean fixture → 100, empty findings, exit 0
- [ ] harness-audit: `--json` schema stable (score, findings[].{kind,path,detail})
- [ ] learnings-harvest: N entries + stub gbrain on PATH → N reported, backend called
- [ ] learnings-harvest: no gbrain → archive JSONL appended atomically
- [ ] learnings-harvest: zero entries / missing .planning → exit 0, `0 harvested`
- [ ] security-surface.sh: extraction refactor — fence behavior unchanged (regression pin)

## TDD Unit Test Map

| Source file | Test file | Functions/behaviors |
|---|---|---|
| scripts/gsd/review-tier.sh | tests/bats/review-tier.bats | tier detection matrix, override, fail-safe default |
| scripts/gsd/security-surface.sh | tests/bats/security-model-fence.bats (extended) | pattern list unchanged post-extraction |
| lib/gates.py (findings-queue) | lib/tests/test_findings_queue.py | add/list/resolve/dedup/roundtrip |
| scripts/hooks/delegation-enforcer.sh | tests/bats/delegation-enforcer.bats | inject/passthrough/off/fail-open |
| scripts/gsd/liveness-check.sh | tests/bats/liveness-check.bats | 8-row truth table + garbage input |
| scripts/harness-audit.py | tests/test_harness_audit.py (pytest) | fixtures: dangling link, drift, clean; json schema |
| scripts/gsd/learnings-harvest.sh | tests/bats/learnings-harvest.bats | backend stub, archive fallback, empty |
| skills/code-uplift/SKILL.md (--slop-only) | tests/bats/code-uplift-slop.bats | green-baseline wall refusal (lever part only) |

## Integration Tests

- INT-001: review-gate SKILL.md references review-tier.sh and prints tier header — grep-pinned by bats (skill-prose contract test, setup-install.bats precedent).
- INT-002: findings-queue writes into the SAME store file gates.py preflight/grant use; `gates.py pending` output unaffected (cross-subcommand isolation).
- INT-003: setup.sh installs all new scripts + hooks dir with exec bits (extend tests/bats/setup-install.bats).
- INT-004: feature-implement/code-uplift SKILL.md finish tails name learnings-harvest.sh step (grep-pinned).
- INT-005: adopt-wip SKILL.md consults liveness-check.sh before abandonment verdict (grep-pinned).
- INT-006: fix SKILL.md diagnose step carries gsd-debug routing criteria (grep-pinned).

## Phase breakdown

### Phase 1 — testable levers (wave-parallel, no cross-deps)
delegation-enforcer.sh [OMC DELEGATION-ENFORCER] · security-surface.sh extraction
+ review-tier.sh [OMC verification-tiers] · liveness-check.sh [AO termination
guardrails] · learnings-harvest.sh [ruflo post-task capture + gsd-extract-learnings]
· harness-audit.py [ruflo MetaHarness ADR-150] · gates.py findings-queue [AO
nudge reducer]. All RED-first.

### Phase 2 — skill-prose wiring (depends on Phase 1)
review-gate tier preamble + findings-queue recording [AC-004/5/7] ·
feature-implement + code-uplift finish-tail learnings step [AC-003] ·
code-uplift `--slop-only` [AC-009, OMC ai-slop-cleaner] · fix→gsd-debug routing
[AC-010] · adopt-wip liveness consult [AC-011 tail] · preflight harness-audit
section [AC-008 tail].

### Phase 3 — docs + install + ship
setup.sh installer additions · CHANGELOG v4.5.0 · README counts ·
docs/fable-pilotfish-alignment.md addendum · full-suite gate · review-gate ·
PR → merge → re-vendor into openclaw + `~/.claude/skills` sync.

## Phase Test Gates

| Phase | Gate condition | Command |
|---|---|---|
| Phase 1 | new lever tests green + shellcheck + pytest | `bats tests/bats/review-tier.bats tests/bats/delegation-enforcer.bats tests/bats/liveness-check.bats tests/bats/learnings-harvest.bats && python3 -m pytest lib/tests/test_findings_queue.py tests/test_harness_audit.py -q && shellcheck scripts/gsd/*.sh scripts/hooks/*.sh` |
| Phase 2 | prose contracts pinned + no regressions | `bats tests/bats/ && python3 -m pytest -q` |
| Final | full suite at baseline+ | `python3 -m pytest -q && bats tests/bats/` (baseline: pytest 231, bats 64 — both must be ≥ baseline, 0 failures) |

## Risks

- Hook JSON contract (PreToolUse stdin/stdout modify semantics) varies by
  Claude Code version — enforcer must passthrough-on-any-doubt (fail-open,
  advisory) so a contract change degrades to today's behavior, never blocks.
- findings-queue store writes race with concurrent gsd runners — reuse
  gates.py's existing store locking/atomic-write path; do not add a second
  writer discipline.
- review-tier LIGHT mode must never swallow the honest-verifier pass when a
  spec is resolvable — tier scopes the DEFECT passes only; verifier stays.
