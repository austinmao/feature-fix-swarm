# Spec 008 — Hardening + observability sweep (G4/G6/G7/G9/G10/G11/G12)

Status: GAUNTLET-AMENDED (2 adversarial reviewers: codex 15 findings incl. 4
CRITICAL, opus 12 findings incl. 1 CRITICAL — all adjudicated in plan.md's
Decision Audit Trail; every REQ below is post-amendment)
Branch: `008-hardening-observability` (from main @ a8866f7)
Prior art: `.planning/prior-art/spec-008-hardening-observability.md` (opus
red-team REQ tables — amended where the cited seams turned out not to exist)
and `specs/008-hardening-observability/prior-art.md` (OSS search:
build-fresh everywhere).

## Context

Track 0 built the wall cap (G3), tamper CI (G2), and the evidence-store pin
(G5). Specs 006/007 are quarantined — their guardrails are parked, not
delivered. This spec closes the remaining red-team guardrails that stand on
seams which exist on main TODAY; the gauntlet verified every seam live and
this spec descopes what has no seam (deploy-pipeline rollbacks, daily-budget
scheduling, org audit-log APIs). G1 declined by operator — do not build. The
two ground-truth incidents shaping G4: the week-long codex burn (array-root
schema bug read as "vendor unavailable") and the jq rc=97 validator bug —
both CONTRACT bugs masquerading as vendor unavailability, detectable only by
a per-rung success floor.

Non-negotiables (do NOT build): HMAC evidence store; required PR review on
main; any daemon/service; G1; daily-budget scheduler (descoped — see
REQ-501); N-parallel scaling before G12 lands.

## Requirements

### G4 — degradation counted, not noted (REQ-101, REQ-102, REQ-103)

- REQ-101 (recording, central): `adversary-host.sh` records outcomes
  CENTRALLY — around every attempt inside `adversary_invoke_model_ladder`
  AND the exact-request path, so no caller can forget to record. Two event
  kinds, distinct schemas (codex C2): RUNG-ATTEMPT
  `{rung_id = vendor:model:effort, outcome = ok|fail}` and INVOCATION
  `{run_id, seam, degraded = bool}` (degraded = the cross-vendor request was
  answered by the active-host fallback). Recorded via
  `gates.py note-degraded` (new subcommand; evidence store, `_StoreLock`
  serialized). Probe/schema failures count as rung `fail` — that is exactly
  the incident class the tripwire exists for. `note-degraded` without a
  run_id for invocation events → usage error; rung-attempt events are
  global (cross-run) by design.
- REQ-102 (ratio rule): a run whose degraded-invocation ratio — degraded
  invocations / all recorded invocations for that run_id, across ALL seams
  (plan-wall rounds, review-gate, qa-coverage; denominator pinned in
  AC-001) — is strictly >50% may MERGE but may NOT promote to prod. The
  rule is ONE shared precondition applied at BOTH prod seams: inside
  `check_grant_prod` (before the hotfix dispatch is NOT possible — hotfix
  routes earlier; see exemption) and inside the `promote` /
  `record_promotion` path (codex C4: direct promote must not bypass it).
  Typed reason `DEGRADED-REVIEW-RATIO` + ratio + remedy. EXEMPTION,
  documented: `hotfix:prod-*` keeps its existing audited bypass channel —
  the hotfix path is posture-knob domain (spec 006) and already leaves a
  durable audit trail; the ratio rule does not extend it (opus F3
  adjudicated: document, not wire).
- REQ-103 (tripwire): a rung whose trailing 20 RUNG-ATTEMPT outcomes are
  all `fail` (≥20 samples) is TRIPPED: one alarm line (G12 event class) and
  the selection filter — applied centrally in `adversary-host.sh` where
  `ordered_rungs` is built AND for exact requests (codex C1) — skips it.
  NEVER filter the last remaining rung: with every rung tripped, selection
  proceeds untripped-order and the existing REVISE/"both hosts unavailable"
  behavior stands (opus F2: the "mandatory HIGH finding" lives in
  review-gate's contract, not the shell — EDGE-013 restated). Recovery:
  HALF-OPEN probe — a tripped rung is retried once every 10th selection
  opportunity (bounded cost), and a recorded `ok` (probe or otherwise)
  clears it; `note-degraded --reset-rung <id>` is the operator override
  (codex C3: auto-clear must be reachable).

### G6 — waiver symmetry (REQ-201, REQ-202)

- REQ-201: FOUR kill-switch seams write a durable waiver before honoring
  the skip, failing CLOSED (gate does not skip; nonzero + typed reason) if
  the waiver cannot be written: `CANARY_GATE=off`,
  `CANARY_GATE_ALLOW_STALE=1`, `PLAN_ADVERSARY=off`, `QA_COVERAGE=off`.
  `DELEGATION_ENFORCER=off` is DROPPED from the set (opus F4 / codex C5:
  the hook is a documented fail-open advisory — a fail-closed waiver would
  invert its contract and block spawns on a store hiccup; its off-switch
  disables advice, not a gate). One shared recorder
  (`scripts/gsd/waiver-record.sh` extracting plan-wall's pattern; plan-wall
  migrates to it — single implementation, grep-count asserted). Waiver row:
  `{run_id, gate, env_var, ts}`; run_id from `$GSD_RUN_ID`, absent →
  literal `unattributed` (the mark matters more than attribution; pinned
  per-seam in AC-003).
- REQ-202: `credential-output-guard.sh` gains a deterministic
  `--scan-file <path>` mode (today it only parses PreToolUse command JSON —
  codex C13); the handoff-writing skills' pre-commit seam runs it over
  handoff docs/registries (guard absent → warn+continue; finding →
  fail-closed). Its own `CREDENTIAL_OUTPUT_GUARD=off` joins the REQ-201
  waiver set (fifth switch).

### G7 — canary→artifact binding (REQ-301, REQ-302)

- REQ-301: canary evidence is TYPED and carries the identity under test:
  `canary-gate.sh` (the trusted wrapper — never third-party results.json
  parsing for identity; codex C6) records the HEAD commit sha it already
  computes into a typed canary evidence record alongside the existing
  pass/console/network contract and the results start/end timestamps.
  `check_promotion` for a surface with canary evidence requires the TYPED
  record and exact-matches its sha against the promoted artifact's sha
  (commit-sha binding — FFS has no image digests; opus F5); canary record
  without a sha → refuse (fail-closed). Freshness rule unchanged and still
  enforced — identity binds IN ADDITION to freshness, never instead.
  DESCOPED from the prior-art text: the "≥10min real traffic + error-rate
  threshold" clause — FFS canaries are browser QA sessions with no traffic
  to measure (codex C7); the recorded timestamps make a future window rule
  one comparison, and the descope is recorded here so it is not mistaken
  for an omission.
- REQ-302 (mechanism, not rehearsal): a `rollback_dryrun` typed evidence
  schema `{run_id, surface, command, exit_code, artifact_sha, ts}` and a
  promote-path rule: a surface that DECLARES a rollback command (none on
  FFS today — the gate no-ops here by construction) requires a fresh
  (same-run) successful dry-run record before promotion.
  `fallback-rehearsal.sh` is NOT the lever (it smoke-tests model CLIs —
  opus F6/codex C9); the schema + gate are the deliverable, exercised by
  synthetic-surface tests, consumed by repos that have real deploys.

### G9 — handoff docs: fence + store authority (REQ-401, REQ-402)

- REQ-401 (honest scope — a prompt-boundary convention, not an injection
  proof; opus F12/codex C14): (a) a shared fence FUNCTION
  (`scripts/gsd/fence-data.sh`: emit `<TAG>_DATA_START/END` with
  counterfeit-delimiter neutralization of the PRIMARY markers in every
  branch — closing plan-wall's own gap, which today neutralizes only
  SOCRATIC markers); (b) the collect/ingest scripts that pipe doc bytes
  into model prompts (inventoried at plan time from actual readers, not
  assumed: spec-status's collector confirmed; others per inventory) route
  through it; (c) the STORE-AUTHORITY rule, narrow and testable: grants,
  budgets, and any authority-bearing value are read ONLY from their stores
  by run_id — a doc line shaped like a grant mints nothing, asserted by a
  fixture ingest + byte-identical ledger. Handoff docs legitimately carry
  resume COMMANDS for the operator to paste — the rule constrains
  authority, not content (codex C11 adjudicated).
- REQ-402: fence-presence + store-authority are verified structurally
  (fence helper exists, consumers call it — grep/verify-skill-blocks
  level), and the ACs claim exactly that: presence + authority isolation,
  NOT model-behavioral injection resistance.

### G10 — budget returns a value (REQ-501)

- REQ-501: `inc_tokens` detects the CROSSING (pre/post comparison — fires
  once, not on every call past the limit; codex C12) and returns a typed
  breach; `cmd_update --tokens` prints a machine-readable
  `BUDGET-BREACH: <run_id> <limit> <spent>` line on crossing; the
  gsd-run.sh drive loop — the seam that invokes run-state updates — reads
  it and applies the SPEC response: finish current task, ship green,
  quarantine the rest (typed reason). DESCOPED: the daily budget + "stop
  starting new specs" scheduler — no daily aggregate store and no
  scheduler seam exist (opus F7/codex C12); recorded as out-of-scope with
  the breach event feeding G12 so a future scheduler has its signal.
  EDGE-009 (double-cross) is descoped with it.

### G11 — global finisher lock (REQ-601)

- REQ-601: extract the COMPLETE primitive — acquire / stale-lease reclaim /
  symlink refusal / release, which live in `acquire_run_state`
  (gsd-run.sh:336+), NOT in the 10-line `claim_pidfile` (opus F9/codex) —
  into a parameterized sourceable helper; gsd-run keeps behavior via the
  helper (single implementation). Finisher tail claims HOST-scoped
  `~/.cache/feature-fix-swarm/finisher.lock` (documented: per-machine —
  multi-machine estates are not serialized, matching current reality). A
  second finisher WAITS up to `FINISHER_LOCK_WAIT` (default 60s), then
  exits 0 AFTER recording a `finisher-skipped` event carrying its run/PR
  (G12-visible — skipped cleanup is marked, never silently dropped; opus
  F10).

### G12 — notification contract (REQ-701, REQ-702, REQ-703)

- REQ-701 (immediate mode, defined mechanics — codex): `scripts/gsd/
  digest.sh --immediate` is a CURSOR-based poll: a durable cursor (last
  emitted event ts per class, stored beside the evidence store) makes
  emission idempotent; invocation seams = run-finalizer tail + operator
  cron (documented one-liners; no daemon). Event classes: gate waiver
  (G6), tripped rung (G4), loop cap/quarantine (G3), budget breach (G10),
  finisher-skipped (G11), EVERY prod promotion + rollback, scan-tamper
  finding on a merged commit, and branch-protection/workflow drift — via
  `gh api repos/<r>/branches/main/protection` + workflows tree sha diffed
  against a recorded baseline (the org audit-log API 404s on personal
  repos — opus F8; verified live). Transport: stdout, plus optional
  `DIGEST_NOTIFY_CMD` (operator-configured command fed lines on stdin);
  notify failure → cursor NOT advanced (redelivery next poll), exit still
  0 — observability never gates.
- REQ-702 (daily mode): specs completed/quarantined, merges w/ shas,
  degraded-review ratio, token spend vs budget, open pendings, stranded
  branch/worktree counts, oldest unmerged PR age. gh unreachable → those
  fields render `unavailable`, exit 0.
- REQ-703 (join keys, honest — codex C15): the LEDGER run_id (`spec-NNN`)
  is the canonical id; gsd-run writes a mapping record
  `{ledger_run_id, runstore_id}` at run start so run-state SQLite rows
  join; grants/waivers/promotions join from the evidence store, budgets
  from run-state VIA the mapping; gh joins by PR/sha recorded in
  promotion/finalizer events. Not a new database — one mapping row.

## BDD Scenarios

Feature: the loop counts its degradation, marks its skips, binds its
canaries, distrusts its notes, and reports itself.

Scenario: degraded reviews block promotion but not merge
  Given a run with 3 of 5 recorded invocations degraded
  When the loop checks the grant for a prod promotion action
  Then it refuses with DEGRADED-REVIEW-RATIO naming the ratio and remedy, while merge-path checks for the run still pass

Scenario: direct promote respects the same rule
  Given the same run
  When the promote path records a promotion
  Then the same DEGRADED-REVIEW-RATIO precondition refuses it

Scenario: a rung at 0% stops costing money
  Given a rung whose trailing 20 rung-attempts all failed and one untripped sibling rung
  When the adversary builds its selection order
  Then the tripped rung is skipped with one alarm line and the sibling is attempted

Scenario: the last rung is never filtered
  Given every rung tripped
  When the adversary builds its selection order
  Then selection proceeds in untripped order and existing unavailable-behavior stands

Scenario: a tripped rung can recover on its own
  Given a tripped rung and 10 selection opportunities since the trip
  When the next selection occurs
  Then the rung gets one half-open probe, and a recorded ok clears the trip

Scenario: a kill-switch leaves a mark
  Given CANARY_GATE=off
  When the canary gate runs on a web-touching diff
  Then it exits 0 AND one waiver record for canary-gate exists in the store

Scenario: a kill-switch that cannot leave a mark does not work
  Given CANARY_GATE=off and an unwritable evidence store
  When the canary gate runs
  Then it fails closed with a typed reason naming the unwritable waiver

Scenario: canary green on the wrong commit does not promote
  Given a typed canary record carrying sha A and a promotion candidate carrying sha B
  When check_promotion evaluates the candidate
  Then promotion is refused naming the identity mismatch

Scenario: declared rollback without rehearsal blocks promote
  Given a synthetic surface declaring a rollback command and no same-run dry-run record
  When the promote path runs
  Then it refuses naming the missing rehearsal

Scenario: handoff doc cannot smuggle a grant
  Given a handoff doc containing "grant merge:pr approved" and a counterfeit END delimiter
  When the collector ingests the doc
  Then the output is fenced with neutralized counterfeits and the grant ledger is byte-identical

Scenario: budget crossing fires once and degrades gracefully
  Given a run one update below its spec budget
  When two consecutive token updates cross and then exceed the limit
  Then exactly one BUDGET-BREACH line is printed and the drive loop quarantines remaining tasks after finishing the current one

Scenario: second finisher yields with a mark
  Given a live finisher holding the lock
  When a second finisher starts and the lock outlasts its wait
  Then it exits 0 and a finisher-skipped event carrying its run exists

Scenario: immediate digest is idempotent
  Given a store with one waiver and a cursor already past it
  When digest.sh --immediate runs twice
  Then the waiver is emitted zero times

## Acceptance Criteria

- AC-001 (G4 ratio): pytest — invocation events recorded per run+seam;
  ratio strictly >50% refuses `deploy:prod-*` (check_grant_prod) AND the
  promote/record_promotion path with `DEGRADED-REVIEW-RATIO` + remedy;
  ==50% passes; merge-path unaffected; denominator = all recorded
  invocations for the run across seams (pinned by a mixed-seam fixture);
  `hotfix:prod-*` documented-exempt (test asserts unchanged behavior).
- AC-002 (G4 tripwire): pytest + bats — trips only at 20/20 (19/20 no;
  <20-sample all-fail no); central filter skips tripped rungs in BOTH
  ladder and exact-request paths; last-remaining rung never filtered;
  half-open probe fires on the 10th opportunity; `ok` clears;
  `--reset-rung` clears; alarm emitted once per trip.
- AC-003 (G6): bats — five switches (4 gates + credential-guard off) write
  one waiver each then honor the skip; unwritable store → fail-closed
  nonzero; rows carry {run_id|unattributed, gate, env_var}; single recorder
  implementation (grep-count); plan-wall migrated to it, its waiver
  behavior byte-compatible.
- AC-004 (G7 binding): pytest — typed canary record with matching sha
  passes; mismatched sha refuses; missing sha refuses; UNTYPED legacy
  runner evidence no longer satisfies a canary-required surface (codex C6
  pinned); freshness still enforced independently.
- AC-005 (G7 rollback): pytest — synthetic surface with declared rollback:
  no record → refuse naming surface; other-surface / other-run / failed
  exit_code record → refuse; fresh same-run success → pass; FFS's own
  surfaces (none declared) → no-op.
- AC-006 (G9): bats — fence helper neutralizes counterfeit PRIMARY
  delimiters in every branch; hostile doc fixture (grant text + counterfeit
  END) → fenced output + ledger byte-identical; fence-presence verified
  structurally across inventoried consumers. Claims are presence +
  store-authority, not model-behavior.
- AC-007 (G9 guard): bats — `--scan-file` flags a credential-shaped string
  in a doc fixture; clean doc passes; guard absent at the seam →
  warn+continue; `CREDENTIAL_OUTPUT_GUARD=off` writes a waiver (AC-003
  set).
- AC-008 (G10): pytest — inc_tokens returns None below, typed breach
  exactly ONCE on crossing (idempotent under repeated over-limit updates);
  cmd_update prints the BUDGET-BREACH line on crossing only; bats —
  gsd-run seam quarantines remaining tasks with typed reason.
- AC-009 (G11): bats — extracted lock primitive: two concurrent claimants
  → one acts; waiter exits 0 after FINISHER_LOCK_WAIT with a
  finisher-skipped event recorded; stale (dead-pid) lock reclaimed;
  symlink at lock path refused; gsd-run still green through the helper.
- AC-010 (G12): bats — --immediate emits each contracted class exactly once
  from a seeded store, cursor advances, second run emits nothing; notify-cmd
  failure → cursor retained (redelivery), exit 0; branch-protection/workflow
  drift detected vs seeded baseline via stubbed gh; --daily renders all
  REQ-702 fields, gh-unreachable → `unavailable` + exit 0; empty store →
  "no events" exit 0; zero live network in tests.
- AC-011 (G12 join): pytest — mapping record written at run start;
  digest joins a budget row (run-state) and a waiver (evidence store) to
  one ledger run_id in output.
- AC-012 (cumulative): `python3 -m pytest tests/ lib/ -q` ≥577, zero
  regressions; full-bats ≥ recorded baseline (main tree, excluding
  `.worktrees/` and `.claude/worktrees/`); `shellcheck -S warning` green on
  every new/touched script.

## E2E Test Paths

- PATH-001: seed 3/5 degraded invocations → `check-grant` prod refuses with
  ratio; record 2 more ok invocations → passes (grant+promotion evidence
  seeded); direct `promote` on the degraded run also refuses.
- PATH-002: `CANARY_GATE=off` run → waiver row → `digest.sh --immediate`
  emits it once; second run emits nothing.
- PATH-003: typed canary sha A vs candidate sha B → refused; re-record
  canary at B → passes; synthetic rollback surface blocks until a same-run
  dry-run record exists.
- PATH-004: hostile handoff doc through the inventoried collector → fenced
  output, neutralized counterfeit, ledger byte-identical.

## Edge Cases

- EDGE-001: rung with <20 samples all failing → not tripped, selectable.
- EDGE-002: unknown rung id → recorded (open set).
- EDGE-003: ratio exactly 50% → allowed (strict >).
- EDGE-004: two switches off in one run → two waiver rows, one each.
- EDGE-005: waiver write vs concurrent store writer → `_StoreLock`
  serialized.
- EDGE-006: canary sha stale vs HEAD → freshness rule refuses independent
  of identity match.
- EDGE-007: rollback record for another surface or another run → refused,
  reason names which.
- EDGE-008: doc contains the literal primary fence delimiter → neutralized,
  ingestion completes.
- EDGE-009: DESCOPED with the daily budget (see REQ-501).
- EDGE-010: live-pid lock → wait then yield-with-mark; dead-pid →
  reclaimed.
- EDGE-011: empty/absent store → digest exits 0 "no events".
- EDGE-012: gh unreachable → fields `unavailable`, exit 0, store-sourced
  fields intact.
- EDGE-013 (restated per opus F2): every rung tripped → NO filtering;
  existing both-hosts-unavailable behavior (REVISE at review-gate) stands.
- EDGE-014: invocation event without run_id → usage error; rung-attempt
  events are cross-run by design.
- EDGE-015: repeated over-limit token updates → breach fires exactly once
  (crossing detection, not threshold detection).
- EDGE-016: `GSD_RUN_ID` unset at a waiver seam → row records
  `unattributed`, skip still honored, fail-closed rule unchanged.

## Test Contract Summary

| Layer | Count | Status |
|---|---|---|
| BDD Scenarios | 13 (spec) / 24 with scenarios.md IDs | draft |
| Unit test cases | 9 groups (plan.md Unit Test List) | listed |
| Unit test files | 9 mapped (plan.md TDD map) | mapped |
| Integration tests | 4 (INT-001..004) | defined |
| E2E paths | 4 (PATH-001..004, CLI-observable — no browser surface, no Playwright stubs by design) | defined |

## Out of scope

G1 (declined); HMAC store; required PR review; daemons/services; daily
budget + new-spec scheduler (descoped — REQ-501); real-traffic canary
windows (descoped — REQ-301); model-behavioral injection resistance claims
(G9 is fence-presence + store-authority); N-parallel scaling before G12;
spec 006/007 re-plans.
