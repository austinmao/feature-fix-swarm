# Spec 004 — Model routing for the Claude 5 / GPT-5.6 generation: split the judgment tier, unbreak plan-gauntlet self-review, define degrade behaviour per vendor state

**Status:** draft-r3 (two adversarial rounds adjudicated: opus plan-check + codex sol bounce ×2) · **Branch:** `004-model-routing` · **Date:** 2026-08-03
**Prior art:** `specs/004-model-routing/prior-art.md` (consort two-vendor lifecycle, spec-kit #513, gsd-core v1.9.x — pattern-mined 2026-08-03)

## Context

The current routing was derived when "opus" meant Opus 4.8 and Fable 5 was the
only frontier tier. Three things changed: Opus 5 shipped (Jul 24, $5/$25, 1M
ctx, zero retention) as a near-fable judge at half price; the GPT-5.6 line
(sol/terra/luna) restructured the Codex side; and FFS's own evidence now shows
the plan gauntlet is self-review. This spec re-derives every model assignment
with a source, fixes the self-review defect, and defines degrade behaviour per
vendor-availability state. Implementation is a follow-up run; this spec + plan
must clear the plan gauntlet first.

## Findings (operator-brief §8.2 handback — verified at file:line)

- **F1 (answers brief §1 — outranks everything else):** the plan-check gate IS
  self-review. Codex host: `scripts/gsd/model-equivalents.sh:24-31` collapses
  `*fable*|*opus*` → `gpt-5.6-sol` and `:33-40` maps both to effort `high` —
  gsd-planner and gsd-plan-checker resolve to the literally identical
  model+effort. Claude host: planner `fable` / checker `opus` — different
  models, same vendor, and `adversary-host.sh` is not in the plan-check path.
  The security fence (`scripts/gsd/security-model-fence.sh:60-64`) demotes the
  planner fable→opus while the checker is already opus — literal same-model
  self-review precisely on security-flagged projects. The cross-vendor bounce
  (`scripts/gsd/plan-adversary.sh`) runs only AFTER the checker gate passes,
  only on high-blast keywords, and is structurally advisory: the gsd-core seam
  treats non-zero exit as restore-and-discard (`plan-adversary.sh:12-15`), so
  findings are appended + exit 0 and adjudicated by the same-vendor checker.
- **F2:** the fable|opus→sol alias collapse loses all distinction (one rung,
  one effort). Reverse map deliberately never yields fable (cost) — kept.
- **F3:** dual default sources disagree today: typed
  `templates/model-requests.json` (planner = judgment → `claude-opus-5` via
  `lib/model_requests.py:20-30`) vs the legacy `model_overrides` seeded from
  `templates/gsd-config.base.json` into a project's live
  `.planning/config.json`, which is what `scripts/hooks/delegation-enforcer.sh`
  reads at spawn time (template seeds; live config injects).
- **F4 (answers brief §2 catalog question):** `claude-opus-5` is ABSENT from
  the gsd-core 1.9.1 model catalog
  (`node_modules/@opengsd/gsd-core/gsd-core/bin/shared/model-catalog.json` —
  has claude-fable-5, claude-sonnet-5, claude-haiku-4-5, sol/terra/luna, only
  opus-4-8/4-5). Scope: affects only gsd-core-native catalog-resolution paths
  (per-agent alias overrides bypass the catalog and the base template sets
  `resolve_model_ids: false`); FFS's own resolver carries opus-5. gsd-core's
  `runtimeTierDefaults.codex.opus` also collapses to sol. `effortSurface`
  (v1.9.0 #2481/#2490) and `stale-bake-guard.cjs` (#1688) ARE in the installed
  build; `setup.sh --doctor` surfaces neither and has no model-resolvability
  check.
- **F5:** six roles share one `judgment` tier — the structural cause of F1.
- **F6:** FFS's own eval (`evals/gpt56/results.json`, 2026-08-01, 72/72 gates
  passed) selected `{judgment: high, execution: medium, volume: low}` — with
  xhigh measured and not selected for judgment. Effort is not monotonic here.
- **F7:** the fence has no in-file kill-switch though
  `docs/model-tiers.md:157-167` implies one.
- **F8 (found by this spec's own gauntlet):** `gsd-security-auditor`,
  `orchestrator`, `spec-status` exist in `templates/model-requests.json` but
  not in `model_overrides` — the security-audit spawn ships UNPINNED, against
  `docs/model-tiers.md:164`.
- **F9 (found by this spec's own gauntlet):** `gates.py findings-queue`
  resolve takes no disposition/reason, and `findings_add` deduplicates against
  RESOLVED signatures without reopening — a blindly-resolved finding never
  blocks again even if re-reported. Tier names are also hardcoded in
  `gsd-run.sh:142-150` and `lib/model_eval.py`, not only in the resolver.
- **Brief errata:** `hooks/delegation-enforcer.sh` → actual path is
  `scripts/hooks/delegation-enforcer.sh`. All other brief claims verified
  (gsd-core v1.9.1 latest; Opus 5 $5/$25 Jul 24; consort architecture as
  described, including its own panel-cost caveat).

## Routing design

### Tier table — 3 tiers become 4

| Tier | Claude | Codex | Justification (source class) |
|---|---|---|---|
| **frontier** (new) | `claude-fable-5` | `gpt-5.6-sol` @ xhigh | Long/hard multi-file synthesis: SWE-Bench Pro 80.3 [vendor, contested] vs Sol 64.6; SlopCodeBench thesis: plan quality leverages everything downstream [humanlayer]. Low-volume seat → 2×-cost acceptable. Fable-over-opus at the planner seat = **GUESS → EVAL-A**. Codex xhigh rung = **GUESS → EVAL-B** (F6 measured high at the gates). |
| **judgment** | `claude-opus-5` | `gpt-5.6-sol` @ high | Reviewer/judge seat: best prompt-injection/sabotage resistance + lowest measured misalignment [Anthropic]; zero retention; half fable price; CursorBench within 0.5% of fable [vendor]; AA Intelligence Index 61 [independent]. Codex effort high [F6 measured]. |
| **execution** | `claude-sonnet-5` | `gpt-5.6-terra` @ medium | Terminal-Bench 2.1 80.4 [independent]; terra AA-CAI 77.4 [labelled]. Effort medium [F6 measured]. |
| **volume** | `claude-haiku-4-5-20251001` | `gpt-5.6-luna` @ low | Luna $0.20/$1.20, AA-CAI 74.6 [labelled]; luna effort low [F6 measured]. Haiku side = **GUESS → EVAL-C Claude arm** (incumbent carried without qualifying evidence). **Bounded-context inputs only** — luna MRCR recall 41.3% [labelled]. |

Model IDs are exact resolver IDs (full haiku date suffix — matches
`lib/model_requests.py:28` and the adversary ladder).

Role assignments (`templates/model-requests.json` = canonical): `gsd-planner`
→ frontier. `gsd-plan-checker`, `gsd-debugger`, `gsd-verifier`,
`gsd-code-reviewer`, `gsd-security-auditor` → judgment. `gsd-codebase-mapper`
volume → execution (**GUESS → EVAL-C**, both vendor arms defined there). All
other roles unchanged. `model_overrides` gains the missing
`gsd-security-auditor: opus` pin — a BEHAVIOUR CHANGE (today that spawn is
unpinned, F8): documented in AC-014, justified by the role's existing
judgment-tier declaration, cost bounded by low spawn frequency.

**Diversity invariant:** gauntlet seats maximize diversity down this ladder:
cross-vendor > same-vendor different-model > same-model different-effort.
Residuals are labelled by IDENTITY, never implied away: the wall record
derives `relation = cross-vendor | same-vendor | self` by comparing the
planner's resolved model id/vendor against the selected reviewer's (AC-008).
Codex-host planner-vs-checker (sol@xhigh vs sol@high) is `self`; Claude-host
planner-vs-checker (fable vs opus) is `same-vendor`; the wall exists to add
the missing axis and its record says which axis it actually added.

### One source of truth (precedence)

`templates/model-requests.json` is canonical. Projections:
`templates/gsd-config.base.json` `model_overrides` +
`dynamic_routing.tier_models` (hand-maintained — linted), Codex agent TOMLs
(generated at runtime by `codex-model-sync.sh` — derived, nothing checked-in
to go stale), and the HARDCODED tier sets in `gsd-run.sh` + `lib/model_eval.py`
(F9 — brought under lint by AC-003). Role present in one source but not
another = ERROR unless on an explicit justified allowlist (`orchestrator`,
`spec-status` — not Agent-tool spawns). `dynamic_routing.tier_models` is
tier-keyed with exactly three rungs mapping light/standard/heavy ↔
volume/execution/judgment; **frontier is deliberately unreachable via dynamic
escalation** (escalation must never auto-select the most expensive tier) and
lint asserts exactly this shape.

### Alias map (`scripts/gsd/model-equivalents.sh`)

`fable → gpt-5.6-sol @ xhigh` · `opus → gpt-5.6-sol @ high` (the collapsed
rung regains one bit via the only Codex-side lever above terra) · sonnet/haiku
rungs unchanged · reverse map keeps `sol → opus`, never fable (deliberate:
cost + 2×-usage billing; a Codex-host degrade must not silently select the
most expensive Claude model).

### Wall reviewer selection — algorithm, not matrix

At wall time the reviewer is selected by one algorithm (AC-005), and the
record shows what was actually selected (AC-008):

The wall resolves the PLANNER'S ACTUAL identity first — from the live
`.planning/config.json` override plus the `model-fallback.sh` marker and the
fence marker (never from templates) — so the record cannot claim fable-vs-X
when the fallback demoted the planner to opus (AC-008).

1. Opposite-vendor CLI present → typed judgment dispatch down THAT vendor's
   existing adversary ladder (sol→terra→luna / opus→sonnet→haiku), bounded,
   per-rung provenance. If EVERY opposite-vendor rung fails, CONTINUE to
   rule 2 (cross-vendor exhaustion falls through, it does not stop).
2. Same-vendor ordered-rung dispatch (AC-016 ordered-rungs parameter):
   distinct-model rungs first — every same-vendor model ≠ the planner's
   resolved id, capability-descending — because a DISTINCT model beats a
   stronger copy of the producer (diversity over capability; findings are
   leads, not verdicts). Claude fence case: planner opus → rungs fable,
   sonnet (a fable classifier refusal is just a rung failure). Codex case:
   planner sol@xhigh → rungs terra, luna.
3. Same-model different-effort — LAST resort, only after every distinct-model
   rung failed — `relation: self`.
4. All rungs exhausted → `WALL-UNREVIEWED`: blocking (implementation stays
   blocked; the only ways forward are an operator fix or a RECORDED waiver).

Worked examples (disjoint; illustrative of the algorithm, not a separate
contract): both CLIs, Claude host → codex sol@high reviews (cross-vendor);
codex fully unusable → rule 2 → opus (`same-vendor`, rung trail shows the
cross-vendor exhaustion). Claude-only, planner fable → opus reviews
(`same-vendor`). Claude-only + fence, planner opus → fable reviews
(`same-vendor`); fable refuses → sonnet. Codex-only, planner sol@xhigh →
terra reviews (`same-vendor`); terra AND luna fail → sol@high
(`relation: self`, labelled). fable down (Claude host) → planner runs opus
via the `model-fallback.sh` marker, wall reads the marker and reviews with
fable/sonnet per rule 2 — the record shows opus-vs-fable, not fable-vs-opus.
sol down (Codex host) → planner spawn FAILS VISIBLY (planner spawns have no
ladder — fail-closed); the wall is moot until the planner runs.

Planner outage contract: `model-fallback.sh` covers exactly the fable→opus
substitution it exists for; every other planner-model outage fails the spawn
visibly rather than substituting. Exact requests keep failing closed
everywhere (existing `lib/model_requests.py` contract).

### Operator-brief opinions scorecard

1. **Agree** — F1/F2 verified verbatim. 2. **Agree** — crossing stated:
expensive models sit only at low-volume plan/review seats; the wall adds ONE
judgment-tier call per phase, accepted because the plan gate is the
top-ranked defect and plan phases are low-volume (EVAL-A/B tune later).
3. **Agree** — fence kept with its CURRENT two-role demotion scope (narrowing
was considered and rejected in round 2: it is a consumer-visible behaviour
change, and it cannot fix the collision anyway — the planner is demoted TO
the checker's model); kill-switch added. The fence-case producer≠reviewer
collapse is repaired by selection-algorithm rule 2 (frontier-rung review of
the demoted plan on Claude-only), with the fable-refusal residual degrading
to sonnet, labelled — never silently opus-reviews-opus. 4. **Agree**
— exact pins only where identity is the point. 5. **Agree** — 4 tiers, one
canonical file + linted projections (incl. the hardcoded consumers), zero new
per-agent vendor IDs. 6. **Agree** — F6 is FFS's own internal evidence;
skill-prose over-verification audit included (US9).

### Consort adoption decisions

Adopt: cross-vendor blind panel **at spec authoring only** (US7 — plan phase
already is single-author + refutation, consort's own plan shape; panel is its
costliest part, built last, gated on a fixture measurement). Adopt:
finding-is-a-lead (wall findings enter the findings-queue refute-or-promote
loop, now with dispositions — AC-015). Adopt: minimal shared finding schema
(AC-006). Defer: boot-time scoreboard routing (seed exists in
`evals/gpt56/results.json` + rehearsal.json; follow-up spec). Reject:
same-vendor panel as degrade (shared blind spots — author + refuter instead,
pair assigned in AC-010).

## User Stories

- **US1 (plan wall):** as an operator, I want a fresh-context plan review by
  the most-diverse available reviewer to run on EVERY phase plan between the
  plan-phase revision gate and implementation start, with HIGH/CRITICAL
  findings blocking until refuted or fixed, so the plan gate is never
  self-review-only and never silently skipped.
- **US2 (diversity honesty):** as an operator, I want the wall record to
  derive and persist the actual planner↔reviewer relation (cross-vendor /
  same-vendor / self) from resolved model identities, so narrowed diversity is
  visible in an artifact, not implied by mode names.
- **US3 (waiver auditability):** as an operator, I want `PLAN_WALL=off` (and
  any skip of a wall run) to leave a durable, append-only waiver record —
  reason, plan path+hash, run id, fence-marker state, security-surface match —
  so an unattended bypass is reviewable next morning; an env var alone must
  never make a mandatory review vanish without trace.
- **US4 (canonical source):** as a maintainer, I want lint to fail when any
  routing projection (`model_overrides`, `dynamic_routing.tier_models`, the
  hardcoded tier sets in `gsd-run.sh`/`lib/model_eval.py`) disagrees with
  `model-requests.json` through the tier map, or when a role/tier is missing
  from a projection without an allowlisted justification, so routing sources
  cannot drift or silently unpin.
- **US5 (degrade honesty):** as an operator with one vendor CLI installed, I
  want the wall to follow the selection algorithm and label the outcome
  (`same-vendor`/`self`), and a wall with NO reachable reviewer to block with
  `WALL-UNREVIEWED` rather than pass, so vendor outages narrow review visibly
  and fail closed.
- **US6 (doctor visibility):** as an operator, I want `setup.sh --doctor` to
  surface gsd-core's stale-bake warning, probe configured models for
  resolvability (expansion rule defined in AC-009), and warn per-surface on
  catalog absence — advisory always; the fail-closed unattended gate remains
  preflight.
- **US7 (spec panel):** as an operator, I want spec authoring to offer two
  blind drafts (one per vendor, principal never drafts) synthesized by
  grafting with attribution — built last, default-off behind a fixture
  measurement, with the default flip reserved for a follow-up change.
- **US8 (finding lifecycle):** as an operator, I want findings-queue
  resolution to require a disposition (`refute|fix|waive`) + reason and want a
  re-reported resolved signature to REOPEN, so "resolved" means adjudicated —
  not silenced forever.
- **US9 (effort hygiene):** as a maintainer, I want skill guard prose audited
  for redundant "final verification step" instructions before Opus 5 lands in
  the judgment seat, so over-verification regressions don't ship with the
  routing change.

## BDD Scenarios

Feature: generation-aware model routing with structural producer≠reviewer

Scenario: wall blocks on cross-vendor HIGH finding (US1 happy path)
  Given both vendor CLIs are installed and a phase plan passed the revision gate
  When the plan wall runs
  Then the opposite-vendor reviewer emits schema-valid findings and implementation stays blocked while any HIGH/CRITICAL signature is unresolved

Scenario: refute-or-fix unblocks (US1 + US8)
  Given a wall-recorded HIGH finding blocks a phase
  When the operator resolves it with disposition refute and a reason, and the wall re-runs
  Then the wall exits 0 and the record shows the adjudicated disposition

Scenario: re-reported finding reopens (US8)
  Given a signature previously resolved with disposition refute
  When a later wall run reports the same finding
  Then the signature REOPENS and blocks again

Scenario: wall record labels the relation (US2)
  Given the orchestrating host is Codex, no Claude CLI is present, and the terra and luna rungs both fail
  When the wall accepts the last-resort sol@high rung against a sol@xhigh planner
  Then the wall record derives relation self from the identical model ids, with the distinct-model failures in the rung trail

Scenario: codex-only common path reviews with a distinct model (US2)
  Given the orchestrating host is Codex and no Claude CLI is present
  When the wall runs against a sol@xhigh planner
  Then terra reviews first and the record shows relation same-vendor

Scenario: same-vendor degrade labelled (US5)
  Given only the Claude CLI is installed and the planner ran fable
  When the wall runs
  Then a fresh-context opus review runs and the record shows relation same-vendor

Scenario: fence case gets a frontier reviewer, refusal degrades labelled (US5)
  Given only the Claude CLI is installed and the fence demoted the planner to opus
  When the wall runs
  Then fable reviews the plan (relation same-vendor); if the fable rung refuses, sonnet reviews and the rung trail shows the degrade

Scenario: reviewer exhaustion blocks (US5)
  Given every reviewer rung fails within bounds
  When the wall completes its ladder
  Then it exits blocking with WALL-UNREVIEWED and implementation cannot start without an operator fix or a recorded waiver

Scenario: waiver leaves a durable record (US3)
  Given PLAN_WALL=off is exported on a plan whose content matches the security surface
  When the wall is invoked
  Then it skips, and an append-only waiver record persists the reason, plan hash, run id, security match, and fence-marker state

Scenario: fence trigger + wall (US3)
  Given planning docs match the security-surface keywords
  When the fence demotes the planner and writes its run-scoped marker
  Then the subsequent wall record carries the fence-marker state and any waiver of that wall is flagged waived-security

Scenario: fence kill-switch (US3)
  Given SECURITY_MODEL_FENCE=off is exported
  When the fence lever runs
  Then the config is unchanged and the lever exits 0 noting the kill-switch

Scenario: lint catches projection drift (US4 happy path)
  Given model-requests.json assigns gsd-planner to frontier and model_overrides pins gsd-planner to opus
  When the routing lint runs
  Then it exits non-zero naming the drifted role

Scenario: lint catches a silently unpinned role (US4)
  Given a role present in model-requests.json but absent from model_overrides and not allowlisted
  When the routing lint runs
  Then it exits non-zero naming the unpinned role

Scenario: lint pins the hardcoded tier consumers (US4)
  Given gsd-run.sh's tier handling lacks the frontier tier
  When the routing lint runs
  Then it exits non-zero naming the stale consumer

Scenario: doctor flags dead pin and catalog absence per-surface (US6)
  Given a configured model whose probe fails and another absent from the gsd-core catalog only
  When setup.sh --doctor runs
  Then the report names both — dead pin as probe advisory, catalog case as catalog-absence warning — and doctor exits without blocking

Scenario: spec panel drafts blind (US7 happy path)
  Given both vendor CLIs are installed and SPEC_PANEL=on
  When spec authoring reaches the panel step
  Then two judgment-tier drafts are produced by independent non-inheriting agents, the principal authors neither, and the synthesis records per-idea attribution

Scenario: panel degrades to author+refuter (US7)
  Given only one vendor CLI is installed
  When spec authoring reaches the panel step
  Then no same-vendor panel runs; the assigned author+refuter pair runs instead and the record notes the degrade

Scenario: effort prose audit (US9)
  Given a skill file instructing a redundant final verification step in a judgment-seat guard
  When the effort-hygiene audit runs
  Then the file and line are reported for removal before the routing change ships

## Acceptance Criteria

- AC-001: four tiers — `frontier`, `judgment`, `execution`, `volume` — resolve
  per the tier table in EVERY tier consumer: `lib/model_requests.py`
  (`CODEX_TIERS`/`CLAUDE_TIERS` + the error message that today hardcodes
  `judgment|execution|volume`), `gsd-run.sh` tier handling — BOTH the
  tier→alias reverse map (:142-150 region) AND the forward legacy-alias block
  (:124-134 region, where `GSD_LEAD_MODEL=fable` currently routes to an exact
  `claude-fable-5` request — it must reach frontier-tier semantics, not
  diverge from the tier path) — and `lib/model_eval.py` legal-set. Unknown
  tiers still fail validation. Exact-request semantics byte-compatible.
- AC-002: `templates/model-requests.json` carries the role moves (planner →
  frontier; mapper → execution). `templates/gsd-config.base.json`
  `model_overrides` agrees through the tier map AND gains
  `gsd-security-auditor: opus` (F8 — behaviour change, AC-014).
  `dynamic_routing.tier_models` keeps exactly three rungs mapped
  light/standard/heavy ↔ volume/execution/judgment; frontier is NOT reachable
  via dynamic escalation.
- AC-003: `scripts/lint_model_routing.py` consistency check across ALL
  projections: role-keyed walk (`model_overrides` vs canonical; missing role =
  error unless allowlisted with justification — `orchestrator`,
  `spec-status`); tier-keyed walk (`dynamic_routing.tier_models` must be
  exactly the three-rung shape above; a canonical tier absent from
  tier_models is an error ONLY for the three mapped tiers — frontier's
  absence is asserted, not excepted); hardcoded-consumer check (grep-level
  assertion that `gsd-run.sh` — both the :124-134 forward-alias block and the
  :142-150 reverse map — and `lib/model_eval.py` recognize exactly the
  canonical tier set). Unknown alias anywhere = error (EDGE-004).
- AC-004: `scripts/gsd/model-equivalents.sh`: `codex_equiv_model` unchanged;
  `codex_equiv_effort` splits `*fable*` → `xhigh`, `*opus*` → `high`. Reverse
  map unchanged. Existing caller suites stay green.
- AC-005: new lever `scripts/gsd/plan-wall.sh <PHASE_DIR|PLAN_FILE>`, invoked
  PER PHASE after that phase's revision gate and before its implementation
  starts, at BOTH seams: the `gsd-run.sh` per-phase pre-execution block
  (beside `OWNERSHIP_GATE`, `gsd-run.sh:77`) and the feature-implement
  per-phase wall step (the SKILL.md lever block region, :92-94 — invoked per
  phase, not once per run). Given a phase dir it enumerates ALL `*-PLAN.md`
  plus bare `PLAN.md` (the repo's real plan naming — `gsd-run.sh:88` globs
  `.planning/phases/*/*-PLAN.md`) and every one must clear (aggregate
  blocking). The wall ALWAYS runs (no
  blast tiering); reviewer selected by the §algorithm via `adversary-host.sh`
  typed judgment dispatch; HIGH/CRITICAL findings recorded to findings-queue
  (AC-015 semantics) block via non-zero exit until adjudicated;
  MEDIUM/LOW advisory. Reviewer exhaustion → `WALL-UNREVIEWED`, blocking.
  `PLAN_WALL=off` skips ONLY with a durable waiver record (AC-008); a skip
  that cannot write its waiver record fails closed. The wall independently
  greps the plan content against `security-surface.sh` KEYWORDS (never
  trusting the fail-soft fence as sole security signal) and stamps the result
  into its record. **Fresh-context contract (executable):** each reviewer
  dispatch is a NEW bounded invocation (fresh `codex exec` / fresh
  non-inheriting agent, no session reuse) whose payload contains ONLY the
  fixed review-brief template + the plan content; a test asserts the
  dispatch payload contains nothing else. Never modifies the plan file.
  Residual (documented): a bare interactive `/gsd-execute-phase` outside the
  FFS pipeline is gsd-core native surface FFS cannot wrap without an
  upstream change.
- AC-006: wall findings conform to `schemas/review-finding.schema.json`:
  root = JSON array of objects; required: `severity` (enum
  `CRITICAL|HIGH|MEDIUM|LOW`), `file` (string, repo-relative), `claim`
  (string); optional: `line` (integer ≥1 or null), `repro` (string or null),
  `vendor` (enum `anthropic|openai`), `confidence` (number 0..1);
  `additionalProperties: false`. **An empty array `[]` is a SUCCESSFUL
  zero-findings review (clean pass)** — "empty output" meaning zero
  bytes/non-JSON is what constitutes a rung failure, along with
  schema-invalid JSON (ladder continues; exhaustion → WALL-UNREVIEWED).
  Fixtures: valid array, empty array (pass), zero-byte output (rung
  failure), malformed entry, boundary values.
- AC-007: `security-model-fence.sh`: gains kill-switch
  `SECURITY_MODEL_FENCE=off`; demotion scope UNCHANGED (both roles, as today —
  narrowing rejected as a consumer behaviour change); on trigger additionally
  writes a RUN-SCOPED marker (path under `.planning/run-state/` keyed to the
  run) consumed by plan-wall.sh for record-stamping and waiver flagging.
  Marker is run-scoped by construction — no clearing lever needed (EDGE-005
  covers a stale same-run marker).
- AC-008: wall record + waiver audit. ONE record PER PLAN:
  `.planning/run-state/plan-wall-<phase>-<plan-slug>.json` (numbered suffix
  on collision); a phase's aggregate verdict = worst of its per-plan
  verdicts. Fields: `{planner_model, reviewer_model, relation, rung_trail[],
  verdict, plan_sha256, run_id, security_match, fence_marker, fence_enabled
  (kill-switch state at wall time), cross_vendor_fallback (state of the
  existing `FFS_CROSS_VENDOR_FALLBACK` knob, which can silently narrow the
  wall to same-vendor and must therefore be stamped), waiver?}`.
  `planner_model` is resolved AT WALL TIME from the live
  `.planning/config.json` override + the `model-fallback.sh` marker + the
  fence marker — never from templates — so a fallback-demoted planner is
  recorded truthfully (tested for the fallback and fence cases). `relation`
  derived by identity comparison (same id → `self`; same vendor prefix →
  `same-vendor`; else `cross-vendor`). **Record durability is universal:**
  EVERY terminal path (reviewed-pass, blocked, WALL-UNREVIEWED, NO-PLAN,
  WAIVED) writes its record atomically (tmp+mv) BEFORE the wall exits, and
  record-write failure on ANY path fails closed (non-zero). A `PLAN_WALL=off`
  skip writes `verdict: "WAIVED"` with `waiver: {reason (required non-empty),
  plan_sha256, run_id, waived_security: <security_match OR fence_marker>}`
  and additionally registers a findings-queue entry with disposition `waive`
  so morning review surfaces it.
- AC-009: `setup.sh --doctor` additions (the NEW checks are advisory —
  existing doctor check statuses and exit semantics unchanged):
  (a) surfaces gsd-core stale-bake-guard state; (b) model resolvability —
  expansion rule: canonical tiers resolve to exact IDs for EACH host whose
  CLI is installed; projection aliases resolve via the equivalents map;
  dedupe to a set of (vendor, exact-id); probe each via the new
  side-effect-free `scripts/gsd/model-probe-lib.sh` (extracted from
  `model-fallback.sh`, which re-sources it — behaviour pinned by existing
  bats); doctor forces re-probe past cache TTL (EDGE-006); (c) catalog checks
  PER-SURFACE: absent-from-gsd-core-catalog warning (must fire today for
  `claude-opus-5`) and absent-from-FFS-resolver warning, independently.
- AC-010: spec-authoring panel (LAST phase, default off behind `SPEC_PANEL=on`):
  dual-vendor drafts = judgment tier on each vendor via independent
  non-inheriting agents, same brief, principal never drafts; artifacts
  `specs/NNN/panel/draft-anthropic.md`, `draft-openai.md`, `synthesis.md`
  (principal-authored synthesis scored on coverage / assumptions surfaced /
  failure modes named / testability, per-idea `[voice: …]` attribution).
  Single-vendor degrade: author = judgment tier, refuter = frontier tier
  same vendor (Claude: opus author / fable refuter; Codex: sol@high author /
  sol@xhigh refuter, labelled `self`) — pair assignment is a **GUESS →
  EVAL-D arm**. EVAL-D pass (panel ≥3-of-4 rubric axes over author+refuter)
  AUTHORIZES flipping the default in a FOLLOW-UP change; nothing flips
  automatically.
- AC-011: effort-hygiene audit executed and recorded to
  `specs/004-model-routing/effort-audit.md`: grep-driven review of
  `skills/*/SKILL.md` guard prose for redundant verification instructions,
  findings fixed or explicitly waived (US9).
- AC-012: docs updated in the same change: `docs/model-tiers.md` (4-tier
  table, diversity invariant, wall + selection algorithm),
  `docs/configuration.md` (new knobs: `PLAN_WALL`, `SECURITY_MODEL_FENCE`,
  `SPEC_PANEL`; plus the EXISTING `FFS_CROSS_VENDOR_FALLBACK` documented as a
  wall-affecting knob whose state the wall record stamps), CHANGELOG entry.
- AC-013: every new/changed shell script shellcheck-clean in its OWN phase
  gate and re-checked in the final gate; bats for plan-wall
  (block/adjudicate/reopen/degrade/exhaustion/waiver/multi-plan/kill-switch),
  fence, equivalents, probe-lib; pytest for resolver, lint, queue v2, doctor;
  full suites ≥ pre-edit baselines (recorded to
  `specs/004-model-routing/baseline.txt` as Phase 0), 0 failures.
- AC-014: migration note in PR body listing BOTH behaviour changes
  (security-auditor newly pinned; plan wall introduced — one judgment call
  per phase) plus consumer impact (sync-drift-check flags until re-sync;
  fork-allowlisted copies unaffected; runtime `~/.claude/skills` re-sync) and
  rollback (revert commits; `gsd-config.base.json` from installer backup;
  `PLAN_WALL=off`/`SECURITY_MODEL_FENCE=off` as recorded, non-revert
  neutralizers).
- AC-015: findings-queue v2 (`lib/gates.py`, backward-compatible): `resolve`
  requires `--disposition refute|fix|waive` + non-empty `--reason`; `add` of
  a RESOLVED signature REOPENS it (unresolved again, prior disposition kept
  in history); `add` gains optional flags `--severity`, `--run-id`,
  `--source wall|review-gate`, `--plan <path>` (today's positional
  `file issue` form stays valid); `list` gains `--severity`, `--source`, and
  `--plan` filters; wall blocking consults
  `list --unresolved --source wall --severity HIGH,CRITICAL --plan <plan>` —
  phase-scoped, so one phase's finding never blocks another phase's wall.
  Existing evidence-store keys and prior queue entries untouched.
- AC-016: `adversary-host.sh` dispatch extension (opt-in, no behaviour change
  for existing callers): (a) schema-aware rung acceptance — new optional args
  carry a schema file + validation command; when present, each rung's output
  is validated before the rung is accepted — invalid/empty output = rung
  failure and the ladder continues (codex rungs pass `--output-schema`;
  claude rungs jq-validate); (b) ordered-rungs parameter — the caller passes
  an explicit rung sequence (model[/effort] list) that overrides the built-in
  ladder (selection-algorithm rule 2 needs fable-first for the Claude fence
  case and terra-first for Codex distinct-model review; today's ladder
  functions take only host kinds + request + timeout and can never select
  fable). Both covered by extended adversary-host bats; args absent =
  byte-compatible.

## Eval proposal (assignments not sourceable today — evals/gpt56 shape)

- **EVAL-A (hand-off, highest signal):** frontier model builds fixture-repo
  checkpoints 1..n, execution model takes n+1 under FFS gates; measure
  strict-pass, inherited-regression retention, verbosity accretion. Arms:
  fable-plan/sonnet-exec vs opus-plan/sonnet-exec vs all-sonnet. Decides the
  GUESS: fable-over-opus at the planner seat.
- **EVAL-B:** sol@xhigh vs sol@high on the 18-fixture judgment corpus (the
  frontier Codex rung GUESS; F6 says high sufficed at the gates).
- **EVAL-C (both vendor arms):** mapper-shaped wide-context fixtures —
  Codex arm luna vs terra; Claude arm haiku-4-5 vs sonnet-5; pass criterion:
  the volume model retrieves ≥95% of planted cross-file facts or the mapper
  stays at execution on that host. Decides the mapper GUESS + the haiku
  `[carried]` tag.
- **EVAL-D:** spec-panel fixture — panel vs the AC-010 author+refuter pair on
  the same brief, scored on the AC-010 rubric; a pass authorizes the default
  flip in a follow-up change.

## E2E Test Paths

E2E = bats-driven CLI journeys (no browser surface; Playwright N/A per
spec-003 precedent). PATH shims stub vendor CLIs; the lever under test is
never mocked.

- PATH-001: both-CLI shims, reviewer returns HIGH → wall non-zero, signature
  queued; `resolve --disposition refute --reason …` → re-run exits 0.
- PATH-002: MEDIUM/LOW-only findings → advisory, exit 0, findings recorded.
- PATH-003: claude-only shim → opus fresh-context, record relation
  `same-vendor`.
- PATH-004: fence fixture → run-scoped marker written; wall record carries
  `fence_marker: true`; `SECURITY_MODEL_FENCE=off` → no demotion/marker.
- PATH-005: lint fixtures — drift → non-zero naming role; missing-role
  (non-allowlisted) → non-zero; tier_models wrong shape → non-zero; stale
  hardcoded consumer → non-zero; agreeing → 0.
- PATH-006: equivalents — `codex_equiv_effort fable` → xhigh, `opus` → high;
  existing caller suites green.
- PATH-007: doctor fixtures — dead pin → probe advisory; catalog-absent-only
  (opus-5 shape) → catalog warning; resolver-absent → resolver warning;
  clean → silent; exit 0 all.
- PATH-008: 4-tier resolver — `frontier` per host in model_requests AND
  gsd-run tier handling; unknown tier rejected with 4-tier message; exact
  request unchanged.
- PATH-009: reviewer exhaustion — all rungs fail → WALL-UNREVIEWED non-zero;
  a schema-invalid preferred rung followed by a valid lower rung → lower rung
  accepted with rung_trail showing both.
- PATH-010: multi-plan phase dir — 2 `*-PLAN.md` files (real repo naming),
  one dirty → aggregate block; both clean → pass.
- PATH-011: seam execution — real `gsd-run.sh` pre-phase path (stub executor)
  runs the wall BEFORE the executor spawn, per phase, against real
  `.planning/phases/*/*-PLAN.md` fixtures.
- PATH-012: waiver — `PLAN_WALL=off` on security-matching plan → skip +
  waiver record with `waived_security: true` + findings-queue `waive` entry;
  waiver-record write failure → wall fails closed.
- PATH-013: reopen — resolved signature re-reported by a later wall run →
  reopened and blocking again.
- PATH-014: fence-case reviewer — claude-only shim, planner opus → fable rung
  reviews; fable rung refuses (shim) → sonnet rung, rung trail shows both.
- PATH-015: zero-findings pass — reviewer returns `[]` → wall exit 0, record
  verdict reviewed-pass (empty array is a clean review, not a rung failure).
- PATH-016: cross-vendor exhaustion falls through — both CLIs, every
  opposite-vendor rung fails (shim) → same-vendor rule-2 rungs run, record
  relation same-vendor with the exhaustion in the rung trail.
- PATH-017: record durability — record-write failure (read-only run-state
  dir) on a reviewed-pass path → wall fails closed; repeated for the WAIVED
  path.

## Edge Cases

- EDGE-001: unreadable/missing PLAN.md → fail-closed (`WALL-UNREVIEWED`
  semantics — a mandatory review cannot be dodged by deleting the plan);
  record written with `verdict: "NO-PLAN"`.
- EDGE-002: reviewer rung times out → bounded ladder continues with rung
  provenance; never silent pass.
- EDGE-003: schema-invalid or empty rung output → rung failure (AC-006);
  indistinguishable-from-never-ran is treated as never-ran.
- EDGE-004: unknown alias in any routing source → lint error, not silent
  skip.
- EDGE-005: fence marker present but plan lacks security keywords within the
  same run → wall stamps both facts (`fence_marker: true`,
  `security_match: false`) and prints `FENCE-MARKER-STALE?`; blocking
  behaviour unchanged (wall always blocks on HIGH regardless).
- EDGE-006: probe cache older than TTL → doctor re-probes; never trusts a
  stale OK.
- EDGE-007: `PLAN_WALL=off` → skip ONLY via the AC-008 waiver record; record
  write failure → fail closed.
- EDGE-008: ladder rung dies mid-dispatch (probe raced availability) → next
  rung, provenance line per rung; typed requests only in the wall path.

## Test Contract Summary

| Layer | Count | Status |
|---|---|---|
| BDD Scenarios | 18 | draft |
| Unit test cases | 32 | listed (plan.md) |
| Unit test files (distinct) | 13 | mapped (plan.md) |
| Integration tests | 6 | defined |
| E2E paths (bats journeys) | 17 | stubbed |

## Out of scope

- Implementation of any of the above in this run (spec + plan through the
  gauntlet only, per operator brief §0.3).
- Boot-time scoreboard routing telemetry (follow-up; seed =
  `evals/gpt56/results.json` + rehearsal.json).
- Upstream gsd-core catalog PR adding `claude-opus-5` (candidate, not a
  dependency).
- Re-plumbing effort onto gsd-core `effortSurface` (per-call
  `-c model_reasoning_effort` already invocation-time; candidate later).
- review-gate mechanical migration to the shared finding schema + queue-v2
  source tags (prose-level adoption here; follow-up).
- Wrapping gsd-core's native interactive `/gsd-execute-phase` (upstream
  change; residual documented in AC-005).
- Escalation-path guard for `dynamic_routing.escalate_on_failure` landing a
  producer on its reviewer's model mid-run (documented residual; lint covers
  the static mapping only).
- Windows.
