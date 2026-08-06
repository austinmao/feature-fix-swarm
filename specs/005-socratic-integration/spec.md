# Spec 005 — Vendor socratic + 3-point integration

> Vendor github.com/m4vic/socratic (self-interrogation question bank + decision-card
> packs) as a pinned external dependency, and wire it into three existing seams:
> spec-time self-interrogation, plan-wall arming, and review-gate verification
> arming. Every touchpoint is fail-soft: a tree without `vendor/socratic/` behaves
> exactly as today.

## Context

- Socratic = 15 domain question files (core ~120 tokens each / full), 10 decision-card
  packs, a self-interrogation loop with a materiality stopping rule, and per-domain
  post-build Verification blocks. MIT. Reviewed 2026-08-05 (session review; sources
  cached). Pin target: commit `862b52e898134ba13ac05a43651ba8d1a7f2a28a` (HEAD of
  main at spec time).
- Prior art: `specs/005-socratic-integration/prior-art.md` — adopt-by-vendoring
  decision (user-directed; no local overlap found by scout).
- Existing vendoring convention to mirror: `vendor/prompt-master/pin.json` +
  `scripts/install-prompt-master.sh` + `lib/ffs_installer.py:stage_prompt_master`.
- Integration seams (confirmed): `skills/feature-spec/SKILL.md` (pre-Phase-1),
  `skills/plan-decompose/SKILL.md` Step 3, `scripts/gsd/plan-wall.sh:263-283`
  (`PW_REVIEW_BRIEF` / `_pw_build_prompt`), `skills/review-gate/SKILL.md` Pass 3 +
  honest-verifier.

## Non-negotiable autonomy constraint (operator-mandated)

Socratic's native rule "escalate only authority decisions — budget, vendor,
irreversible actions" MUST NOT produce operator prompts inside FFS autonomous
flows. In `--autonomous` (default for `/feature-implement`) and under MAX-AUTH
`/feature-spec`:

- Every socratic "open question" is resolved by recording a defensible default as
  an `ASSUME-NNN` ledger entry in `socratic.md` — never an interactive stop.
- Authority decisions that map to operator-gated actions become typed grant-ledger
  entries (auto-granted under MAX-AUTH, reviewed under `--gated`) — the EXISTING
  grant machinery, no new stop points.
- Socratic interactive-interview mode is never used by FFS.
- The assumption ledger + would-have-asked list surfaces in the completion
  summary, so the operator reviews decisions after the fact instead of being
  blocked during the run.

## User Stories

### US-1: Pinned vendoring
**As a** FFS maintainer, **I want** socratic vendored at an audited commit with the
same pin/patch/marker convention as prompt-master, **so that** the pipeline consumes
its question files reproducibly and offline, immune to upstream churn.

### US-2: Spec-time self-interrogation artifact
**As a** spec author (or autonomous run), **I want** `/feature-spec` to produce
`specs/NNN/socratic.md` — domain set, self-answered highlights, assumption ledger,
risks, open-questions-routed-to-grants — **so that** domain coverage and assumptions
are explicit inputs to spec/plan instead of vanishing.

### US-3: Domain-armed plan wall
**As a** plan reviewer (cross-model adversary), **I want** the plan-wall and
plan-decompose adversarial prompts armed with the spec's socratic domain slice
(core question files + 0–2 pack cards), **so that** plan review checks a concrete
domain checklist and the assumption ledger instead of generic vibes.

### US-4: Verification-armed review gate
**As a** review-gate verifier, **I want** the in-scope domains' Verification blocks
and the `ASSUME-NNN` ledger injected into Pass 3 / honest-verifier, **so that**
post-build review verifies each recorded assumption held and each domain's
concrete checks ran.

## BDD Scenarios

Feature: Socratic question-bank integration

Scenario: Installer vendors socratic at the pinned commit
  Given a checkout with `vendor/socratic/pin.json` naming commit 862b52e…
  When  the socratic installer runs with a valid destination
  Then  the destination contains `SKILL.md`, `questions/core/`, and `packs/`, plus a `.ffs-socratic.json` marker recording repository, commit, and patch hash

Scenario: Installer refuses an existing destination
  Given a destination path that already exists
  When  the socratic installer runs against it
  Then  it exits nonzero with a message deferring ownership to setup.sh, and the destination is unchanged

Scenario: feature-spec emits the socratic artifact
  Given a repo where `vendor/socratic/` is installed
  When  `/feature-spec` runs for a new spec
  Then  `specs/NNN/socratic.md` exists with domains frontmatter, self-answered highlights, an `ASSUME-NNN` ledger, and top risks — and no interactive question was asked of the operator

Scenario: Autonomous run never prompted by socratic (error-path of the old behavior)
  Given a socratic pass whose questions include an authority decision (e.g. vendor choice)
  When  the pass runs under `--autonomous` / MAX-AUTH
  Then  the decision is recorded as an assumption or typed grant entry and the run continues without an operator stop

Scenario: Plan wall consumes the domain slice
  Given `specs/NNN/socratic.md` with `domains: [requirements, testing, security]`
  When  `plan-wall.sh` reviews that spec's phase plans
  Then  the reviewer prompt contains the three matching `questions/core/*.md` slices and the assumption ledger as delimited untrusted data

Scenario: Fail-soft without the vendor tree
  Given a repo where `vendor/socratic/` is absent
  When  feature-spec, plan-wall, and review-gate run
  Then  each behaves byte-for-byte as before this feature (no socratic.md required, unmodified prompts, exit codes unchanged)

Scenario: Review gate audits the assumption ledger
  Given a diff under review and a `socratic.md` containing `ASSUME-003: single-region`
  When  the review-gate verification pass runs
  Then  the verifier output addresses each ASSUME entry as held / violated / unverifiable, and a violated entry becomes a normal severity-ranked finding

## Acceptance Criteria

- AC-001: `vendor/socratic/pin.json` exists with `repository`, `commit`
  (`862b52e898134ba13ac05a43651ba8d1a7f2a28a`), and optional `patch`;
  `scripts/install-socratic.sh` clones at the pin, applies the patch when present,
  writes `.ffs-socratic.json` (schema `ffs.external-skill/v1`) with `patch_sha256`
  null-or-hash, and refuses existing/unsafe destinations — same contract as
  `install-prompt-master.sh`.
- AC-002: `lib/ffs_installer.py` stages socratic alongside prompt-master
  (`stage_socratic` mirroring `stage_prompt_master:1506`) into
  `.agents/skills/socratic` (canonical) with fingerprint tracking; uninstall and
  doctor treat it as a managed external skill.
- AC-003: `/feature-spec` gains a pre-Phase-1 socratic step producing
  `specs/NNN/socratic.md` with machine-readable frontmatter
  (`domains: [...]`, `depth: core|full`, `packs: [...]`) and sections
  `## Self-answered highlights`, `## Assumed (flag if wrong)` (`ASSUME-NNN:` lines),
  `## Open questions → grants`, `## Top risks`. Depth auto-escalates to `full` on
  auth/payments/PII/production signals (socratic's own rule). Authoring is
  fail-closed: frontmatter values are validated against the known enum
  immediately after writing (typo'd domain never silently drops arming), and
  the generated file carries a header comment documenting the valid enum.
  Skipped/unknown domains surface in the completion summary.
- AC-004: In `--autonomous`/MAX-AUTH flows, the socratic step NEVER calls
  AskUserQuestion or otherwise blocks on the operator; open questions are recorded
  as ASSUME entries and/or typed grant actions. In `--gated` attended mode, open
  questions may be batched into the EXISTING Step-6 review stop only — no new stop
  points anywhere.
- AC-005: `plan-wall.sh` resolves the spec dir from the branch-NNN convention
  (same pattern review-gate uses) and appends, when `specs/NNN/socratic.md`
  resolves, the matching question files and the ASSUME ledger to the reviewer
  prompt as delimited untrusted data (`SOCRATIC_DATA_START/END`), capped at the
  declared domains + ≤2 packs. When the file, vendor tree, or a resolvable
  spec dir is absent — or `SOCRATIC=off` — the prompt is BYTE-identical to
  today (conditional append, no stray newline). Editing socratic.md
  invalidates plan-wall's zero-dispatch idempotence fast path when armed.
- AC-006: `plan-decompose` Step 3 (opposite-host adversarial review) receives the
  same domain slice under the same fail-soft rule.
- AC-007: `review-gate` Pass 3 / honest-verifier receives in-scope domains'
  `## Verification` blocks + the ASSUME ledger; each ASSUME entry gets a
  held/violated/unverifiable verdict, and violated entries flow into the normal
  findings queue with severity.
- AC-008: All three integration points are fail-soft: with `vendor/socratic/`
  absent, every touched skill/script produces its pre-feature behavior (verified by
  tests running both with and without a fixture vendor tree).
- AC-009: Token discipline: a default (core-depth) socratic pass loads only the
  selected domains' core files (~120 tokens each) + SKILL.md — never the full
  question bank; full depth only on AC-003's escalation signals or explicit
  request.
- AC-010: Docs updated: `docs/dependencies.md` gains a socratic section (pin,
  patch policy, upstream-submission convention, concrete pin-bump runbook);
  CHANGELOG entry; `install-socratic.sh` added to the CI installer
  syntax-check lines.
- AC-011 (advisory, not a ship gate): one A/B evidence pass — the plan-wall
  adversary run on a historical phase plan with and without the slice, findings
  diff recorded in `evals/socratic-ab.md`. Empty diff → retro decision is to
  un-arm the three seams.
- AC-012: Observability + escape hatch: `socratic-slice.sh` emits exactly one
  stderr status line per successfully-parsed invocation (exit 0/3); usage
  errors (exit 2) emit usage text only (`armed domains=…` / `skipped
  (<reason>)`), and `SOCRATIC=off` disables arming per-run via the same
  empty-output fail-soft path.

## E2E Test Paths

(CLI repo — E2E = bats + pytest against a fixture git repo; no browser surface.)

- PATH-001: Fresh install path — run installer against a local clone fixture →
  vendored tree + marker verified (`tests/test_installer.py`).
- PATH-002: Spec-pipeline path — drive the feature-spec socratic step against a
  toy spec dir with vendor tree present → `socratic.md` contract sections present,
  zero interactive prompts (`tests/bats/socratic-spec-step.bats`).
- PATH-003: Plan-wall arming path — run `plan-wall.sh` prompt-builder against a
  fixture phase with and without `socratic.md`/vendor tree → armed vs
  byte-identical prompts (`tests/bats/socratic-plan-wall.bats`).
- PATH-004: Review-gate audit path — feed a fixture ASSUME ledger through the
  verification-arming helper → verdicts emitted per entry
  (`tests/bats/socratic-review-gate.bats`).

## Edge Cases

- EDGE-001: `socratic.md` frontmatter malformed — no opening separator, an
  unterminated block, a missing `domains` key, or a `domains` value that is
  not a bracket list — → slice helper emits empty stdout + `skipped
  (malformed frontmatter)` status, exit 0; consumers run unarmed. Absent
  `depth`/`packs` keys default (`core` / `[]`) rather than malform; an
  out-of-enum `depth` value warns and falls back to `core` without
  malforming.
- EDGE-002: Domain name typo in frontmatter → authoring-time enum validation
  fixes it before the pipeline proceeds; if a stale/hand-edited file reaches
  the helper, unknown names are skipped with a warn, known ones still arm.
- EDGE-003: Branch has no NNN prefix (detached HEAD, `main`) when plan-wall
  runs → spec resolution fails → empty slice, byte-identical prompt, no error.
- EDGE-004: `SOCRATIC=off` set mid-pipeline → all three consumers degrade to
  unarmed on that run; status lines record `skipped (SOCRATIC=off)`.
- EDGE-005: Vendor tree present but a declared domain's question file missing
  (partial/corrupt install) → that domain skipped with warn; remaining domains
  still emitted.
- EDGE-006: socratic.md edited between plan-wall runs → idempotence cache
  invalidated (sha folded into key); reviewer sees fresh slice, not stale.
- EDGE-007: >2 in-enum, resolvable packs declared → first two SURVIVING
  packs honored, rest skipped with warn (the enum filter and the
  missing-file filter both run before the cap, so neither an unknown name
  nor a broken install file consumes a cap slot).
- EDGE-008: pin.json names a patch that fails `git apply --check` → installer
  exits nonzero before touching the destination.
- EDGE-009: Existing unmanaged `.agents/skills/socratic` at install time →
  installer refuses (setup.sh ownership rules), same as prompt-master.
- EDGE-010: Autonomous run where socratic surfaces an authority decision →
  recorded as ASSUME/grant entry, never an operator prompt (AC-004); attended
  `--gated` runs batch it into the existing Step-6 stop only.

## E2E Test Stubs

(bats, not Playwright — no browser surface; stubs land in `tests/bats/`.)

```bash
# tests/bats/socratic-plan-wall.bats
@test "PATH-003: armed prompt contains delimited slice for declared domains" {
  # arrange: fixture spec dir with socratic.md (domains: [requirements, testing]),
  #          fixture vendor tree, branch named 005-fixture
  # act:     invoke the prompt-builder path of plan-wall.sh
  # assert:  output contains SOCRATIC_DATA_START, both domain slices, ASSUME lines
}

@test "PATH-003: absent vendor tree yields byte-identical prompt" {
  # arrange: same fixture, vendor tree removed; capture baseline prompt pre-feature
  # act:     rebuild prompt
  # assert:  cmp -s armed-absent.txt baseline.txt
}

# tests/bats/socratic-review-gate.bats
@test "PATH-004: every ASSUME entry receives a verdict token" {
  # arrange: fixture socratic.md with ASSUME-001..003, stub diff
  # act:     run the verify-mode slice + verifier prompt assembly
  # assert:  slice contains all three ASSUME lines inside SOCRATIC_DATA_START/END
}

# tests/bats/socratic-spec-step.bats
@test "PATH-002: socratic.md contract sections present, no interactive prompt" {
  # arrange: toy spec dir, vendor tree fixture
  # act:     run the authoring-validation helper over a generated socratic.md
  # assert:  frontmatter enum-valid; four required sections present
}
```

## Test Contract Summary

| Layer             | Count | Status  |
|-------------------|-------|---------|
| BDD Scenarios     | 7     | draft   |
| Unit test cases   | 23    | listed  |
| Unit test files   | 5     | mapped  |
| Integration tests | 3     | defined |
| E2E paths         | 4     | stubbed |
