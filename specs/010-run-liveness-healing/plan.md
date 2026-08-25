<!-- /autoplan restore point: ~/.gstack/projects/austinmao-feature-fix-swarm/010-run-liveness-healing-autoplan-restore-20260810-122430.md -->
# Plan 010 — Run-liveness healing (v2: durable lifecycle + reconciler)

v1 (five bespoke mechanisms) was reviewed by /autoplan; at the final gate the operator
chose the reviewers' reframe: **one durable run-lifecycle state machine + a one-shot
reconciler**. This plan implements spec.md v2 in three phases, each a ≤300-line gsd plan
(the spec ships the gate that enforces its own discipline).

## Prior-art decision (required citation)

**build-fresh** — adjudicated 2026-08-10, table + rationale in
`specs/010-run-liveness-healing/prior-art.md`. terryso/claude-auto-resume (813★)
vindicated but rejected (inverted architecture, license unverified, fit ~15-25%);
borrowed: the banner-regex *shape* only, one-line attribution comment. `gh run watch`
rejected as the poll core (fixed 3s interval, held connection, no classification/rerun
policy — operator confirmed build-fresh at the gate); `gh run rerun --failed` IS the
rerun primitive. Infra/test keyword-table classification follows three ≥4k-star repos'
CI skills. The lifecycle reframe itself follows the reconciler pattern (persist state,
exit with wake condition, external scheduler reconciles) — standard in k8s-style control
loops, applied here as a one-shot bash verb, no daemon shipped.

## Architecture

See spec.md `## Architecture summary` (authoritative). Component → file map:

| Component | File | Shape |
|---|---|---|
| Lifecycle store | `scripts/gsd/lifecycle.sh` (new) | verbs `checkpoint\|transition\|show\|validate`; atomic tmp+mv JSON per run under `.planning/run-state/lifecycle-<run>.json`; python3 heredoc for JSON (repo precedent: env-registry.sh); durable budgets `{respawns, wakes, ci_reruns}`; illegal-transition table enforced |
| Wall evaluator | `scripts/gsd/plan-wall.sh` `--await <secs>` (new verb) | rc 0 pass-class / 20 decided-blocked / 75 pending; record trust = `.run_id` match + `.plan_sha256` match (eng C1); plan set resolved once; empty set rc 1; read-only proven by byte-identical round counter |
| Time evaluator | inside `scripts/gsd/session-wake.sh` (new, evaluator + parser) | banner parse: capture tail 50 lines, `(limit reached\|hit your limit).*resets` shape (attributed), strict HH:MM validation before arithmetic (eng H3), rollover, cap 21600; emits `wake_at`; `SESSION-WAKE:no-banner` WARN on failing-drive-no-match; NO in-process sleep — produces the checkpoint payload |
| CI evaluator | `scripts/gsd/ci-watch.sh` (new, evaluator) | one bounded probe per call: pinned `databaseId`, attempt-gated classification (eng M1), infra keyword table → rerun under durable `ci_reruns ≤ 2`, test failure terminal, gh-failure = idle with consecutive cap; ALSO usable standalone as a bounded backoff watch loop (30s→600s, total `FFS_CI_WATCH_MAX_SECS=7200`) for attended/skill use |
| Reconciler | `scripts/gsd/reconcile.sh` (new) | one idempotent pass under the spec-009 coord claim; waiting+satisfied → runnable → relaunch resume_argv (budget decrement) → typed line; quarantined/done/failed untouched; `FFS_RECONCILE=off` no-op |
| Worker wiring | `scripts/gsd/gsd-run.sh` (edit, post-drive ~:1310-1324) | respawn-or-checkpoint: rc 124 respawn regardless of commits; other rc + zero commits (git probe fail-CLOSED, eng H5) respawn; quarantined never (eng C2); log append `>>` + procsub wait + RESPAWN line into log (eng H4); attempt 2 = remaining time (eng M2); banner in capture tail → checkpoint waiting(time) + exit (no sleep, no lock held — eng C3/H1/H2 resolved by NOT sleeping at all) |
| Plan gate | `scripts/gsd/plan-length-gate.sh` (new) | unchanged from v1; hard rc 1 per operator gate decision |
| Skills | `skills/feature-implement/SKILL.md` `## Wall await rule`; `skills/spec-decompose/SKILL.md` gate step; 7 entrypoint skills get an opportunistic reconcile line beside the Init gate | lint: `tests/test_host_dispatch_lint.py` named-list assertions |
| Docs | `docs/healing.md` (new) + README row + CHANGELOG | states/transitions/budgets/kill-switches/typed-lines/exit-codes; operator cron line; vendor sunset notes; n=1 caveat; success metric for spec-011 retro |

Design notes carried from review: the exec-chain problem (eng C3 — pidfile, heartbeat,
EXIT traps across a 6h in-process sleep) is DISSOLVED by checkpoint-and-exit: no process
sleeps, locks release naturally through the existing exit path, and `sleeping` vs working
ambiguity (H1) disappears because a waiting run has no process at all — its state is the
lifecycle record. TERM-deferral (H2) likewise moot. The forged-banner blast radius (H3)
shrinks to one bogus `wake_at` value, bounded by the wake-attempt budget and visible in
the record.

## Unit Test List

Sequenced design-critical first:

- [ ] lifecycle: checkpoint writes atomic valid JSON; validate accepts it
- [ ] lifecycle: illegal transition quarantined→runnable → rc 1 typed
- [ ] lifecycle: illegal transition done→waiting → rc 1 typed
- [ ] lifecycle: budget decrement survives simulated re-invocation (no reset)
- [ ] lifecycle: concurrent checkpoint (two writers) → last-write-wins atomically, never a torn file
- [ ] await: all PASS-class verdicts → rc 0 first poll
- [ ] await: mixed pass/blocked terminal set → rc 20 (never rc 0)
- [ ] await: undecided at 2s deadline → rc 75, no record created
- [ ] await: stale plan_sha256 → pending (C1)
- [ ] await: foreign run_id record → pending (C1)
- [ ] await: round-counter file byte-identical before/after
- [ ] await: empty plan set → rc 1 typed, no wait
- [ ] await: foreign-cwd invocation still resolves records (decoy-store)
- [ ] respawn: rc 124 + ≥1 commit → respawns (idempotent-resume contract)
- [ ] respawn: rc 1 + zero commits → respawns once
- [ ] respawn: rc 1 + ≥1 commit → no respawn, rc propagates
- [ ] respawn: status=quarantined → NO respawn (C2)
- [ ] respawn: git probe error → NO respawn, fail closed (H5)
- [ ] respawn: attempt 1 log lines survive attempt 2 (H4)
- [ ] respawn: MAX=0 → disabled; both-die → final rc, one RESPAWN line
- [ ] banner: parseable reset → correct wake_at epoch; past time → tomorrow
- [ ] banner: matched but invalid HH:MM shape → unparseable rc 1, no arithmetic (H3)
- [ ] banner: failing drive, no banner → SESSION-WAKE:no-banner WARN rc 0
- [ ] banner: banner outside tail-50 → not matched (forgery scope)
- [ ] reconcile: satisfied time condition → relaunch, argv verbatim, budget decremented
- [ ] reconcile: unsatisfied → record untouched (byte-identical)
- [ ] reconcile: quarantined/done/failed → untouched
- [ ] reconcile: second concurrent pass loses coord claim → typed rc 0, no double launch
- [ ] reconcile: budget-exhausted relaunch → record failed + typed budget line
- [ ] reconcile: FFS_RECONCILE=off → typed no-op
- [ ] ci: pinned databaseId not re-resolved when second run appears (M1)
- [ ] ci: classification waits for attempt increase after rerun (M1)
- [ ] ci: infra failure → one rerun, durable counter 1/2
- [ ] ci: third infra failure → rerun-exhausted, record failed
- [ ] ci: test failure → terminal immediately, zero reruns
- [ ] ci: gh nonzero exit → idle poll, bounded consecutive cap
- [ ] plan-length: 301 → rc 1 named; 300 → rc 0 silent; two violations both named
- [ ] plan-length: empty dir → rc 1 typed; env override honored

## TDD Unit Test Map

| Source file | Test file | Behaviors |
|---|---|---|
| scripts/gsd/lifecycle.sh | tests/bats/lifecycle.bats | schema/transitions/atomicity/budgets |
| scripts/gsd/plan-wall.sh (--await) | tests/bats/plan-wall-await.bats | rc contract/trust/read-only/foreign-cwd |
| scripts/gsd/gsd-run.sh (respawn+checkpoint) | tests/bats/gsd-run.bats (extend) | respawn matrix/quarantine/log-append/checkpoint-on-banner |
| scripts/gsd/session-wake.sh | tests/bats/session-wake.bats | parse/validate/rollover/no-banner/tail-scope |
| scripts/gsd/ci-watch.sh | tests/bats/ci-watch.bats | pin/attempt-gate/rerun-budget/terminal/gh-failure (stubbed gh) |
| scripts/gsd/reconcile.sh | tests/bats/reconcile.bats | relaunch/untouched/claim-race/budget-fail/off |
| scripts/gsd/plan-length-gate.sh | tests/bats/plan-length-gate.bats | over/at/multi/empty/override |
| skills/*/SKILL.md sections | tests/test_host_dispatch_lint.py (extend) | await-rule + gate + reconcile-line assertions |

## Integration Tests

- INT-001: PATH-002 — drive exits 124, zero commits → one respawn, stub succeeds, status
  running→completed (real vocabulary), log intact across attempts.
- INT-002: PATH-003 — banner capture → waiting(time) record + clean exit; reconcile after
  wake_at relaunches stub resume with verbatim argv.
- INT-003: PATH-004 — stubbed gh two-pass reconcile: rerun then relaunch-on-success.
- INT-004: PATH-006 — quarantined + foreign-claimed records untouched across a pass.
- INT-005: PATH-005 + docs command sweep — every command in docs/healing.md executes.

## Phase Test Gates

| Phase | Gate condition | Command |
|---|---|---|
| Phase 1 (store + wall evaluator + plan gate) | new bats + shellcheck | `bats tests/bats/lifecycle.bats tests/bats/plan-wall-await.bats tests/bats/plan-length-gate.bats && shellcheck -S warning scripts/gsd/lifecycle.sh scripts/gsd/plan-wall.sh scripts/gsd/plan-length-gate.sh` |
| Phase 2 (evaluators: banner + ci) | new bats + shellcheck | `bats tests/bats/session-wake.bats tests/bats/ci-watch.bats && shellcheck -S warning scripts/gsd/session-wake.sh scripts/gsd/ci-watch.sh` |
| Phase 3 (reconciler + gsd-run wiring + skills + docs) | bats + lint + full pytest | `bats tests/bats/reconcile.bats tests/bats/gsd-run.bats && python3 -m pytest tests/test_host_dispatch_lint.py -q && python3 -m pytest -q` |
| Final | full suite delta vs baseline | `python3 -m pytest -q && bats tests/bats/` |

## Implementation phases

1. **Phase 1 — durable state + the dominant fix**: `lifecycle.sh` (store, schema,
   transitions, budgets), `plan-wall.sh --await` (trusted-record evaluator),
   `plan-length-gate.sh`, `## Wall await rule` in feature-implement, decompose gate
   wiring, lint tests. Ships the ~29h-class fix first (codex #12 ordering).
2. **Phase 2 — the other evaluators**: `session-wake.sh` (banner→wake_at parser),
   `ci-watch.sh` (pinned/attempt-gated/budgeted), their bats.
3. **Phase 3 — close the loop**: `reconcile.sh`, gsd-run respawn-or-checkpoint wiring,
   entrypoint opportunistic reconcile lines, `docs/healing.md` + README + CHANGELOG,
   integration tests, edge-coverage refresh.

Ordering strict 1→2→3 (2's evaluators feed 3's reconciler; 3 documents the whole).

## Risks

- gsd-run.sh post-drive surgery (1324-line live file): Phase 3 pins existing
  rc-propagation behavior in bats BEFORE the edit; respawn loop stays ~30 lines around
  the existing drive call; extraction-to-Python deferred (audit trail #10 note).
- Reconciler double-launch: coord claim + idempotent transitions; tested (claim-race bats).
- Banner forgery: tail-scope + strict shape + durable wake budget; bounded to one bogus
  wake_at per budget unit; visible in the record.
- Cron absence: without the operator cron line, reconciliation happens only at FFS
  entrypoint invocations — documented honestly in docs/healing.md ("a waiting run wakes
  when the operator next touches FFS, or on the cron cadence if installed").
- n=1 evidence: stated in spec + docs; spec-011 retro instruments the success metric.

## GSTACK REVIEW REPORT (/autoplan, 2026-08-10)

Phases run: CEO (dual voices: Claude Opus subagent + codex exec, both fresh-context) →
Eng (Claude Opus subagent; codex's CEO output carried eng-grade findings 6-11, merged) →
final gate. Design phase skipped (no UI scope). DX phase skipped (surface = SKILL.md
sections + typed lines; downstream wall/review-gate re-review with fresh context).
Premise gate: passed via prior operator approval (ExitPlanMode on
humble-percolating-clock.md + /continue-compact re-confirmation) — not re-asked.

CEO DUAL VOICES — CONSENSUS TABLE:
| Dimension | Claude | Codex | Consensus |
|---|---|---|---|
| Premises valid? | partly (compliance-vs-mechanism unnamed) | no (n=1 extrapolation) | DISAGREE-with-plan → n=1 stated; mechanism cause verified (600s tool ceiling vs 180s/plan wall) |
| Right problem? | items 2,3 yes; 1 reframed; 4,5 doubted | reframe to durable lifecycle | USER CHALLENGE → operator CHOSE the reframe (v2) |
| Scope calibration? | cut ~40% | ship item 1 + telemetry first | Phase 1 ships the dominant fix first; full scope kept as lifecycle components |
| Alternatives explored? | foreground-wall skipped | foreground-wall skipped | RESOLVED: tool ceiling makes pure-foreground impossible ≥3 plans; foreground stays the 1-2-plan default |
| Ecosystem risk? | items 1,3,4 shadow vendor roadmap | same | CONFIRMED → sunset lines in docs |
| Evidence quality? | n=1, not in repo | n=1, no P50/P95 | CONFIRMED → stated in spec + docs; retro instruments the metric |

ENG DUAL VOICES — CONSENSUS TABLE:
| Dimension | Claude | Codex (CEO-phase eng findings) | Consensus |
|---|---|---|---|
| Architecture sound? | item 3 contract undefined | monolith + in-process recovery | RESOLVED by v2 checkpoint-and-exit (C3/H1/H2 dissolved) |
| Test coverage? | weak rulers named | no blocked-wall e2e proof | 40-test list incl. rc-20, trust, race, budget paths |
| Performance risks? | respawn doubles bound | slot-holding sleep | M2 adopted; sleeping process eliminated entirely |
| Security threats? | forged banner + forged verdict | forged banner | C1 record trust + H3 strict parse + tail scope |
| Error paths? | fail-open guards | process-local caps | H5 fail-closed + durable lifecycle budgets |
| Deployment risk? | installer glob verified fine | — | CONFIRMED safe |

### Gate outcome (operator, 2026-08-10)

1. USER CHALLENGE → **"Reframe to state machine now"** — v2 of spec.md/plan.md is that
   reframe: lifecycle store + wake-condition evaluators + one-shot reconciler; no daemon
   shipped (one-shot verb + entrypoint opportunistic pass + documented operator cron).
2. TASTE plan-length → **hard rc 1 at 300** (confound documented).
3. TASTE ci-watch core → **build-fresh backoff poll** (no held connection; custom
   pin/attempt/budget logic needs it anyway).
4. **Approved** — proceed to decompose → preflight → grant → implement.

### Decision Audit Trail

| # | Phase | Decision | Class | Principle | Rationale |
|---|---|---|---|---|---|
| 1 | CEO | premise gate = prior operator approval | mechanical | P6 | approved plan + /continue-compact args |
| 2 | CEO | rc contract split for --await (0/20/75) | mechanical | P5 | zero never overloaded |
| 3 | CEO | respawn on rc124 regardless of commits | mechanical | P1 | idempotent resume |
| 4 | CEO | durable run-wide budgets | mechanical | P1 | process-local caps are not bounds |
| 5 | CEO | no-banner WARN line | mechanical | P1 | fail-safe ≠ fail-quiet |
| 6 | CEO | tail-only banner parse | mechanical | P5 | bounds forgery |
| 7 | CEO | n=1 stated in spec/docs | mechanical | P5 | evidence honesty |
| 8 | CEO | vendor sunset lines in docs | mechanical | P3 | cheap future deletion |
| 9 | Eng | C1 record trust (run_id+sha) | mechanical | P1 | forged/stale records = pending |
| 10 | Eng | C2 quarantine guard | mechanical | P1 | budget breach is terminal |
| 11 | Eng | C3→v2 checkpoint-and-exit | mechanical (post-reframe) | P5 | dissolves exec-chain/sleep hazards |
| 12 | Eng | H4 log append + procsub wait | mechanical | P1 | attempt 1 evidence preserved |
| 13 | Eng | H5 fail-closed git probe | mechanical | P1 | error ≠ zero commits |
| 14 | Eng | M1 pin + attempt-gate + gh-failure idle | mechanical | P1 | no double-burned reruns |
| 15 | Eng | M2 attempt-2 remaining budget | mechanical | P3 | caller bounds respected |
| 16 | Eng | M3 status vocabulary fix | mechanical | P5 | test asserts real strings |
| 17 | Gate | lifecycle reframe accepted | user-challenge | — | operator decision |
| 18 | Gate | plan-length hard rc 1 | taste | P6 | operator decision |
| 19 | Gate | ci-watch build-fresh poll | taste | P3 | operator decision |
| 20 | Gate | approved → decompose | mechanical | P6 | pipeline continues |
