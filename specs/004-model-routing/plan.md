# Plan — spec 004 model routing (Claude 5 / GPT-5.6 generation) · r4

## Prior-art decision (required citation)

Per `specs/004-model-routing/prior-art.md`: **PORT** — adopt three consort
patterns as FFS-native implementations (blind panel at spec authoring, shared
finding schema, finding-is-a-lead verification); consort itself is a Claude
Code plugin with a competing lifecycle, not adoptable as a dependency. Defer
its scoreboard pattern. gsd-core v1.9.1 stays exact-pinned (confirmed latest;
effortSurface + stale-bake-guard already installed). spec-kit #513 confirms
the direction, ships no artifact.

## Architecture notes

- **Wall placement**: the gsd-core `workflow.plan_bounce_script` seam
  restores-and-discards on non-zero exit (`plan-adversary.sh:12-15`) — a
  blocking wall cannot live there. `scripts/gsd/plan-wall.sh` is FFS-owned,
  invoked PER PHASE at both verified seams: `gsd-run.sh` per-phase
  pre-execution block (beside `OWNERSHIP_GATE`, `gsd-run.sh:77`) and the
  feature-implement per-phase wall step (SKILL.md lever-block region,
  `skills/feature-implement/SKILL.md:92-94` — extended to run per phase,
  after that phase's plan resolution). Plan discovery uses the repo's REAL
  naming: `*-PLAN.md` + bare `PLAN.md` (`gsd-run.sh:88` globs
  `.planning/phases/*/*-PLAN.md`); PATH-011 executes the real gsd-run.sh
  path against real-named fixtures (grep-pins alone can pass dead wiring).
  Residual: bare interactive `/gsd-execute-phase` outside FFS is gsd-core
  native surface — documented, not wrapped.
- **No blast tiering**: the wall always runs; HIGH/CRITICAL always block.
  review-tier.sh is NOT reused (it classifies git diffs, which don't exist at
  plan time). The wall's own `security-surface.sh` KEYWORDS grep over plan
  CONTENT feeds record-stamping and waiver flagging only.
- **Planner identity is resolved at wall time** from live
  `.planning/config.json` + the `model-fallback.sh` marker + the fence
  marker — never templates — so records stay truthful under fallback/fence
  demotion (r3 CRITICAL).
- **Reviewer selection**: rule 1 opposite-vendor ladder (falls THROUGH to
  rule 2 on exhaustion); rule 2 same-vendor ORDERED distinct-model rungs
  (diversity over capability — fable→sonnet for the Claude fence case,
  terra→luna for Codex); rule 3 same-model different-effort last resort
  (`self`); rule 4 WALL-UNREVIEWED blocking. Needs AC-016's ordered-rungs
  parameter (today's ladder can never select fable). `[]` = clean
  zero-findings pass; zero-byte/non-JSON/schema-invalid = rung failure.
- **Blocking state = findings-queue v2** (AC-015): dispositions + reopen +
  severity/run/source/plan metadata + filtered list (`--plan` scoping so one
  phase's finding never blocks another). Same store, same locking.
- **Record durability**: one record per plan
  (`plan-wall-<phase>-<plan-slug>.json`), atomic tmp+mv on EVERY terminal
  path before exit; write failure fails closed. Waivers additionally enter
  the queue with disposition `waive`.
- **Fresh-context contract**: every dispatch = new bounded invocation,
  payload = fixed review brief + plan content only; asserted by test.
- 4-tier resolver touches EVERY tier consumer: `lib/model_requests.py` (rows +
  error string), `gsd-run.sh` — BOTH the :124-134 forward legacy-alias block
  (fable must reach frontier semantics) AND the :142-150 reverse map —
  `lib/model_eval.py` legal-set. Lint (AC-003) pins all of them + both JSON
  projections + the tier_models three-rung shape (frontier deliberately
  unreachable via dynamic escalation).
- **Probe library**: `scripts/gsd/model-probe-lib.sh` factored out of
  `model-fallback.sh` (which today mixes probing with top-level config
  mutation); fallback re-sources it (behaviour pinned by existing bats);
  doctor sources it side-effect-free, AC-009 expansion rule, TTL re-probe.
- Finding schema `schemas/review-finding.schema.json` per AC-006.
- Panel (AC-010) is LAST and default-off; EVAL-D pass authorizes a follow-up
  default flip only.

## Test Strategy note (E2E adaptation)

CLI skill suite — no browser surface; E2E = bats journeys per PATH-NNN
(spec-003 precedent). Vendor CLIs stubbed via PATH shims; the lever under
test is never mocked. **Phase 0 records pre-edit baselines** (`python3 -m
pytest -q` count + `bats tests/bats/` count) to
`specs/004-model-routing/baseline.txt` — a task artifact, not a preflight
probe (the full suite exceeds the 30s probe timeout, `lib/gates.py:1061`).
Preflight probes a scoped fast subset and asserts AT LEAST ONE vendor CLI
resolves (single-vendor hosts must pass).

## Unit Test List

Sequenced design-critical first (32 cases):

- [ ] resolver: `frontier` → claude-fable-5/None, gpt-5.6-sol/xhigh
- [ ] resolver: `judgment` unchanged; unknown tier rejected with 4-tier message; exact round-trip byte-compatible
- [ ] gsd-run tier handling: frontier lead model + effort on both hosts (reverse map :142-150)
- [ ] gsd-run forward-alias block (:124-134): `GSD_LEAD_MODEL=fable` reaches frontier semantics, no divergence from tier path
- [ ] model_eval legal set includes frontier
- [ ] equivalents: `codex_equiv_effort fable`→xhigh, `opus`→high; model + reverse maps unchanged
- [ ] lint: drift fixture → non-zero naming role
- [ ] lint: missing-role (non-allowlisted) → non-zero (F8 class)
- [ ] lint: tier_models wrong shape / frontier reachable → non-zero
- [ ] lint: stale hardcoded consumer (gsd-run both blocks / model_eval) → non-zero
- [ ] lint: agreeing full fixture → 0; unknown alias → error (EDGE-004)
- [ ] wall: HIGH finding → non-zero + queued; resolve refute+reason → re-run 0
- [ ] wall: MEDIUM/LOW only → advisory exit 0
- [ ] wall: `[]` reviewer output → clean pass, verdict reviewed-pass (PATH-015)
- [ ] wall: claude-only shim, planner fable → opus reviews, relation same-vendor
- [ ] wall: claude-only + fence, planner opus → fable rung; fable refusal shim → sonnet, trail shows both (PATH-014)
- [ ] wall: codex-only, planner sol@xhigh → terra first, relation same-vendor; terra+luna fail → sol@high relation self
- [ ] wall: both CLIs, opposite-vendor rungs all fail → falls through to same-vendor rule 2 (PATH-016)
- [ ] wall: all rungs fail → WALL-UNREVIEWED non-zero (PATH-009)
- [ ] wall: zero-byte rung output → rung failure; valid lower rung accepted, rung_trail both
- [ ] wall: missing PLAN.md → NO-PLAN fail-closed, record written (EDGE-001)
- [ ] wall: phase dir, 2 `*-PLAN.md`, one dirty → aggregate block, per-plan records (PATH-010)
- [ ] wall: PLAN_WALL=off → WAIVED record + queue waive entry; record-write failure → fail closed (PATH-012)
- [ ] wall: record durability on reviewed-pass and WAIVED paths with read-only run-state → fail closed (PATH-017)
- [ ] wall: planner identity from live config + fallback marker (opus-fallback planner recorded as opus, not fable) + fence marker case
- [ ] wall: fresh-context — dispatch payload contains only brief + plan
- [ ] wall: security-keyword plan → `security_match: true` stamped independent of fence; `fence_enabled` + `cross_vendor_fallback` stamped
- [ ] fence: SECURITY_MODEL_FENCE=off → unchanged, exit 0; trigger → run-scoped marker + existing two-role demotion regression pin
- [ ] queue v2: resolve without disposition/reason → error; re-add of resolved signature REOPENS with history; severity/source/plan filters; unrelated store keys untouched
- [ ] probe-lib: side-effect-free sourcing; TTL-stale → re-probe; model-fallback bats stay green
- [ ] doctor: dead-pin advisory / catalog-absent (opus-5 shape) / resolver-absent / clean silent; new checks advisory, existing statuses unchanged
- [ ] schema fixtures: valid, empty-array pass, zero-byte fail, malformed fail, boundary values, additionalProperties rejected; adversary-host ext: ordered-rungs + schema args per-rung validation, args absent byte-compatible

## TDD Unit Test Map

| Source file | Test file | Behaviors |
|---|---|---|
| lib/model_requests.py | tests/test_model_requests.py (extended) | frontier row, error message, validation regression |
| scripts/gsd/gsd-run.sh (both tier blocks) | tests/bats/gsd-run.bats (extended) | frontier lead/effort, forward-alias convergence |
| lib/model_eval.py | tests/test_model_eval.py (new or extended) | legal set |
| scripts/gsd/model-equivalents.sh | tests/bats/model-equivalents.bats (extended) | effort split, maps unchanged |
| scripts/lint_model_routing.py | tests/test_model_routing_lint.py (extended) | drift / missing-role / tier_models / hardcoded consumers / allowlist / unknown alias |
| scripts/gsd/plan-wall.sh + schemas/review-finding.schema.json | tests/bats/plan-wall.bats (new) | block/adjudicate/selection order/degrade/exhaustion/waiver/durability/multi-plan/fresh-context/identity/stamps |
| scripts/gsd/security-model-fence.sh | tests/bats/security-model-fence.bats (extended) | kill-switch, marker, demotion regression pin |
| scripts/gsd/model-probe-lib.sh | tests/bats/model-probe-lib.bats (new) + tests/bats/model-fallback.bats (regression) | side-effect-free, TTL, fallback pin |
| lib/gates.py (findings-queue v2) | lib/tests/test_findings_queue.py (extended) | dispositions, reopen, filters incl. --plan, store isolation |
| lib/ffs_installer.py (doctor) | tests/test_installer.py (extended) | stale-bake surface, probe expansion, per-surface catalog warnings |
| scripts/gsd/adversary-host.sh (ordered-rungs + schema ext) | tests/bats/adversary-host.bats (extended) | ordered-rungs selection, per-rung validation, back-compat |
| panel lever (Phase 4) | tests/bats/spec-panel.bats (new) | blind-draft isolation, degrade pair, artifact shape |

(13 distinct test files.)

## Integration Tests

- INT-001: `gsd-run.sh` per-phase block invokes plan-wall.sh beside
  OWNERSHIP_GATE — grep-pin PLUS PATH-011 execution test.
- INT-002: feature-implement SKILL.md per-phase wall step names plan-wall.sh
  — grep-pinned.
- INT-003: setup.sh installs plan-wall.sh, model-probe-lib.sh, schemas/ with
  correct bits (extend tests/bats/setup-install.bats).
- INT-004: fence marker → wall record stamps `fence_marker: true` + waiver
  flags `waived_security` end-to-end (same sandbox).
- INT-005: lint green against the SHIPPED templates (post-AC-002
  self-consistency).
- INT-006: queue integration — wall-recorded signature visible to filtered
  `findings-queue list --plan`; resolve unblocks; reopen re-blocks; unrelated
  evidence keys untouched.

## Phase breakdown

### Phase 0 — baselines
Record pre-edit pytest + bats counts → `specs/004-model-routing/baseline.txt`.

### Phase 1 — resolver + maps + lint (RED-first)
4-tier resolver in ALL consumers (AC-001: model_requests + both gsd-run
blocks + model_eval) · equivalents effort split (AC-004) · role moves +
security-auditor pin + tier_models shape (AC-002) · lint (AC-003).

### Phase 2 — queue v2 + adversary extension + wall + fence + schema
findings-queue v2 (AC-015) · adversary-host ordered-rungs + schema validation
(AC-016) · plan-wall.sh + per-plan records/waivers (AC-005/AC-008) · finding
schema (AC-006) · fence kill-switch + run-scoped marker (AC-007) · probe-lib
extraction (AC-009 prerequisite).

### Phase 3 — doctor + docs + hygiene
doctor additions (AC-009) · effort-hygiene audit → effort-audit.md (AC-011) ·
docs + CHANGELOG (AC-012) · setup.sh install rows (INT-003).

### Phase 4 — spec panel (LAST, default-off)
Panel lever behind `SPEC_PANEL=on` (AC-010) + EVAL-D fixture harness.
Building the measurement IS the done-condition; no default flip here.

### Phase 5 — ship
Full-suite gate ≥ baseline (incl. shellcheck sweep) · review-gate · PR with
migration note (AC-014) → merge → re-vendor consumers + runtime
`~/.claude/skills` sync.

## Phase Test Gates

| Phase | Gate condition | Command |
|---|---|---|
| Phase 0 | baseline artifact exists | `test -s specs/004-model-routing/baseline.txt` |
| Phase 1 | resolver/lint/equivalents + shellcheck | `python3 -m pytest tests/test_model_requests.py tests/test_model_routing_lint.py tests/test_model_eval.py -q && bats tests/bats/model-equivalents.bats tests/bats/gsd-run.bats && shellcheck scripts/gsd/model-equivalents.sh scripts/gsd/gsd-run.sh` |
| Phase 2 | queue/wall/fence/probe-lib/adversary + shellcheck | `python3 -m pytest lib/tests/test_findings_queue.py -q && bats tests/bats/plan-wall.bats tests/bats/security-model-fence.bats tests/bats/model-probe-lib.bats tests/bats/model-fallback.bats tests/bats/adversary-host.bats && shellcheck scripts/gsd/plan-wall.sh scripts/gsd/model-probe-lib.sh scripts/gsd/security-model-fence.sh scripts/gsd/adversary-host.sh` |
| Phase 3 | doctor + install + docs assertions | `python3 -m pytest tests/test_installer.py -q && bats tests/bats/setup-install.bats && test -s specs/004-model-routing/effort-audit.md && grep -q PLAN_WALL docs/configuration.md && grep -q "spec 004" CHANGELOG.md` |
| Phase 4 | panel fixture + EVAL-D artifact | `bats tests/bats/spec-panel.bats && test -s evals/spec-panel/eval-d-results.json` |
| Final | full suite ≥ baseline + shellcheck sweep | `python3 -m pytest -q && bats tests/bats/ && shellcheck scripts/gsd/*.sh scripts/hooks/*.sh` (vs baseline.txt; 0 failures) |

## Migration note (lands in PR body per AC-014)

- **Changes:** 4-tier table (frontier); planner→frontier, mapper→execution,
  security-auditor newly pinned (F8 — behaviour change); codex effort split
  fable=xhigh/opus=high; always-on plan wall (behaviour change — one
  judgment-tier call per phase; waivers durable-recorded); fence kill-switch
  + run-scoped marker (demotion scope unchanged); findings-queue v2
  dispositions/reopen/metadata; adversary-host ordered-rungs + schema
  validation (opt-in); doctor model checks; finding schema; probe-lib
  extraction.
- **Breaks/flags for consumers:** synced levers flagged by sync-drift-check
  until re-synced; fork-allowlisted copies unaffected; runtime
  `~/.claude/skills` re-sync; consumers that spawned gsd-security-auditor
  unpinned now get opus; every phase pays one wall review.
- **Rollback:** revert the implementation commits;
  `templates/gsd-config.base.json` restores from installer backup;
  `PLAN_WALL=off` + `SECURITY_MODEL_FENCE=off` neutralize the new gates
  without a revert (wall-off leaves waiver records by design).

## Risks

- Wall cost: one judgment-tier call per phase, every phase — the deliberate
  price of closing F1 (top-ranked defect); EVAL-A/B inform later tuning; the
  waiver path exists and is audited rather than silent.
- Distinct-model-first review (terra reviewing a sol plan) trades reviewer
  capability for diversity — the spec's stated preference (findings are
  leads); if EVAL data later shows capability matters more, the ordered-rungs
  parameter makes the swap a one-line change.
- frontier-seat GUESSes (fable-over-opus, xhigh) — EVAL-A/EVAL-B before
  tuning cost expectations.
- findings-queue v2 must stay backward-compatible with existing store
  consumers (preflight/grant/promote paths) — INT-006 + store-isolation unit
  case pin this.
- `dynamic_routing.escalate_on_failure` can still land a producer on its
  reviewer's model mid-run — documented residual; lint covers the static
  mapping only.
- gsd-core catalog lacks `claude-opus-5` (F4) — per-surface doctor warning
  keeps it visible; upstream PR candidate, not a dependency.
