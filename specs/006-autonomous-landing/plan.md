# Plan 006 — Autonomous landing

## Prior-art decision

`specs/006-autonomous-landing/prior-art.md`: **build-fresh**. Vindicated OSS
merge queues (bors-ng 1531★, homu 214★) own the merge decision themselves and
carry no grant ledger, no autonomous per-item implement step, no takeover
record — categorical architecture mismatch, nothing to adopt/port/wrap.
Internal reuse is maximal instead: `collect-estate.py` wholesale,
`liveness-check.sh`, `run-finalizer.sh` merged-head proof, `gates.py`
grant/pending machinery. Design of record:
`.planning/prior-art/spec-006-autonomous-landing.md` (REQ tables verbatim in
spec.md).

## Architecture

Three phases, one spec (a split would strand phase 2 behind phase 1's merge —
the exact failure this spec kills).

```
Phase 1                       Phase 2                        Phase 3
/spec-status v1.2.0 ──┐   collect-queue.py (intake) ──┐   git-branch-consolidate
  writes takeover/    │       │ union 3 sources        │     Step 4-A --autonomous
  <run-id>.json+.md   │       ▼                        │     (4-part conjunction)
takeover-check.sh ────┤   /land-queue SKILL.md         │   autonomy.posture knob
  9 invariants, <5s   │       │ per item: rebase →     │     zero|floor (config+env)
feature-implement ────┘       │ feature-implement      │   docs/autonomy-posture.md
  Step 2: 1 wall line         │ --autonomous → CI wait │   promotion-protocol Rule 12
                              │ → merge → finalize     │
                          queue-guard.sh (caps+breaker)┘
```

Key mechanics:
- **Takeover record** = index, never truth: the wall re-evaluates every invariant
  live. Written beside evidence.json (`_store_path`-resolved — inherits G5's
  worktree pinning landed in PR #91). `forbid[]` is built exclusively from
  deterministic probes; the spec-status scouts' prose can never mint an entry.
- **Wall order** (cheapest-first, fail-fast): store identity → single-flight
  (O_EXCL lockfile beside the store — NOT liveness-check.sh, whose ship-grant
  signal self-deadlocks queue items; REQ-103) → clean-or-identical-dirty git
  state → branch identity → dirty-set not grown → check-preflight → check-grant
  each → findings 0 HIGH/CRIT → poison preconditions. Every refusal prints its
  one-command remedy. Terminal: TAKEOVER-OK | TAKEOVER-REFUSED:<reason>. >72h
  (min of created_at and file mtime, cross-checked vs store granted_at) → hard
  refusal. `--list` serves cold-session discovery.
- **Queue guards own their counters** in `queue-guard.sh` (mirrors `loop-round`'s
  store idiom incl. the fail-distinct infra-error rc — EDGE-006). Guards ship in
  the SAME plan as the loop; `--parallel` ships last and is cut first.
- **Consolidate 4-A** executes only on the 4-part conjunction; ALL deletion
  delegates to run-finalizer's merged-head proof — grep-able single path.
- **Posture knob** read order: `FFS_AUTONOMY_POSTURE` env (STRICTER-only — may
  select floor over a zero config, never zero over floor; garbage → advisory +
  fall through) → `.planning/config.json` `autonomy.posture` → default `"zero"`.
  Effective posture + provenance printed once per run (REQ-306).

## Self-answered highlights
- Takeover `.md` is a prose superset for humans; `.json` is the only machine
  input — the wall never parses markdown. Stored `resume.command` strings are
  DISPLAY-ONLY: consumers re-derive actions from typed fields, never eval or
  execute a stored string.
- `collect-queue.py` is stdlib-only Python (json/subprocess/pathlib), mirroring
  collect-estate.py; no new deps.
- CI wait sources `run_bounded` from `scripts/gsd/run-bounded.sh` (NOT
  adversary-host.sh) and passes SECONDS (`run_bounded 1200 …` — the python3
  fallback rejects "20m"). Its no-timeout-tool path returns 124 without running
  the command; preflight therefore probes for a timeout tool so BLOCKED:ci-timeout
  can never mean "never ran".
- `<store>` = the store DIRECTORY: `$(python3 gates.py store-dir)` (new
  accessor over `_store_path().parent`) — evidence.json is a FILE; nothing
  nests under it.

## Assumed (flag if wrong)
- collect-estate.py's JSON output shape (default stdout; no --json flag exists)
  is stable — pinned by its own tests.
- liveness-check.sh is NOT touched by this spec (the wall uses its own O_EXCL
  lock; liveness-check keeps serving its existing gsd-run consumers).

## Top risks (ranked, from design red-team)
1. /land-queue becomes the next uncapped loop → guards in same plan, REQ-204..207.
2. Autonomous consolidate deletes real work → conjunction + delta assertion +
   finalizer-only deletion + never-unlockable refusals (REQ-301..304).
3. Stale record trusted → live re-verification + 72h hard refusal (EDGE-001).
4. MAX-AUTH widening via --rearm / consolidate:estate → both derive from
   deterministic artifacts; REQ-104/305 pin the boundary.
5. --parallel run-state races → REFUSED in v1 entirely (REQ-214/EDGE-007);
   grammar reserved for a future spec with lease-grade isolation.

### Prompt-injection posture
Takeover records and queue jsons are DATA. Skills read them via jq/python
field extraction, never eval; the wall treats record values as claims to
re-verify, not instructions. Handoff-doc fencing (G9) is spec 008's scope —
here the machine path simply never consumes prose.

## Implementation phases

### Phase 1 — Takeover record + wall (US-1: REQ-101..106)
1. `skills/spec-status/SKILL.md` v1.2.0 + `scripts/collect-status-facts.sh`
   `== TAKEOVER ==` block: deterministic probes emit the record json + md next to
   the evidence store.
2. `scripts/gsd/takeover-check.sh` (new): 9 invariants, --rearm, --json.
3. `skills/feature-implement/SKILL.md` Step 2: one wall line after
   check-preflight (no-op without record; fail-closed under --autonomous).
4. `tests/bats/takeover-check.bats` + fixture record factory.

### Phase 2 — /land-queue (US-2: REQ-201..214)
1. `skills/land-queue/scripts/collect-queue.py` + `tests/test_collect_queue.py`
   (intake/ordering/precheck first — pure logic, TDD RED first).
2. `scripts/gsd/queue-guard.sh` + `tests/bats/queue-guard.bats` (guards BEFORE
   the loop that uses them).
3. `skills/land-queue/SKILL.md` (grammar, per-item execution, terminal states,
   Human inbox, no-token-meter note) + `tests/bats/land-queue.bats`.

### Phase 3 — Consolidate 4-A + posture + docs (US-3: REQ-301..306, AC-D01..03)
1. `skills/git-branch-consolidate/SKILL.md` Step 4-A `--autonomous` +
   `tests/bats/consolidate-autonomous.bats`.
2. `templates/gsd-config.base.json` `autonomy.posture` + env override + posture
   resolution helper (where consumers read it).
3. `docs/autonomy-posture.md` (new) + `docs/promotion-protocol.md` Rule 12
   revision + `docs/commands.md` + CHANGELOG.

## Unit Test List

Sequenced design-critical first:
- [ ] collect_queue.union: 3 sources merge, (branch, head-SHA) dedupe; disagreeing sources → BLOCKED:identity-conflict (REQ-201)
- [ ] collect_queue.union: empty sources → empty queue, exit 0
- [ ] collect_queue.union: docs-only branch (landed==true, disposition docs-only) still enters the queue (REQ-201)
- [ ] collect_queue.order: overlapping computed file sets → same group, oldest first; transitive A∩B,B∩C,A∩C=∅ chains into one group (REQ-202)
- [ ] collect_queue.order: disjoint items → ascending residual file count
- [ ] collect_queue.precheck: already-landed → SKIPPED, zero subprocess model calls (REQ-203)
- [ ] collect_queue.precheck: branch gone + head reachable from base → reconciled LANDED; gone without proof → BLOCKED:source-missing (REQ-203)
- [ ] collect_queue.precheck: merge-tree conflict after 1 rebase attempt → BLOCKED:conflict
- [ ] takeover-check: each of 9 invariants violated → its exact TAKEOVER-REFUSED reason (REQ-103, 9 cases)
- [ ] takeover-check: all hold → TAKEOVER-OK exit 0
- [ ] takeover-check: record >72h → refusal regardless of invariants (EDGE-001)
- [ ] takeover-check --rearm: record ∩ store grant-history only; branch-file-only action NOT re-armed (REQ-104)
- [ ] takeover-check --rearm: stale preflight → re-run; failure names secret NAME only (EDGE-009)
- [ ] spec-status record: schema keys present (REQ-101); scout-prose poison → 0 forbid entries (REQ-102)
- [ ] queue-guard: round 3 on same item → trip (REQ-204)
- [ ] queue-guard: item/queue clocks + max-items trip (REQ-204)
- [ ] queue-guard: same normalized sig twice → BLOCKED:no-progress (REQ-205)
- [ ] queue-guard: 2 consecutive same-class → QUEUE-ABORTED:systemic:<class> (REQ-206)
- [ ] queue-guard: corrupt store → distinct infra error, never a guard verdict (EDGE-006)
- [ ] land-queue: red item → queue continues (REQ-207); --resume skips LANDED (REQ-208)
- [ ] land-queue: hanging gh checks → rc124 → BLOCKED:ci-timeout, merge NOT called (REQ-210)
- [ ] land-queue: reviewer probe fails at intake → floor: QUEUE-ABORTED:no-reviewer; zero: degradation counted (REQ-209)
- [ ] land-queue: crash injected between merge and LANDED append → resume reconciles to LANDED, no second merge (REQ-208)
- [ ] land-queue: head SHA drift between CI pass and merge → BLOCKED:head-moved (REQ-210)
- [ ] land-queue: empty check suite / merge-conflict / gh-auth-failure fixtures → named BLOCKED, no merge (REQ-210)
- [ ] land-queue: STOP file mid-queue → QUEUE-ABORTED:operator-stop; --drain finishes current item (REQ-211)
- [ ] land-queue: second concurrent queue → QUEUE-REFUSED:queue-live (REQ-212)
- [ ] land-queue: identity-conflict on disagreeing sources → BLOCKED:identity-conflict, no Frankenrecord (REQ-201)
- [ ] land-queue: hostile record (wrong types / path traversal / symlink / oversize) → schema refusal (REQ-213)
- [ ] land-queue: quarantine requeues once after base advance, parks on second (EDGE-010)
- [ ] takeover wall: NO record present → no-op, existing feature-implement path untouched (REQ-105 second half)
- [ ] takeover wall: identical dirty-set PASSES (EDGE-003 pass case)
- [ ] takeover wall: two concurrent walls → exactly one proceeds (EDGE-004)
- [ ] consolidate 4-A: each missing evidence item → refuse (REQ-301, 4 cases)
- [ ] consolidate 4-A: set mismatch — equal-count wrong-set substitution → exit 1 (REQ-302)
- [ ] consolidate 4-A: target ref advanced after proof → refuse, report drift (REQ-302/304)
- [ ] consolidate 4-A: refused-action matrix under full grant — EXECUTED refusals, not greps (REQ-303, 5 cases)
- [ ] consolidate 4-A: --report-only default prints plan, deletes nothing (REQ-301)
- [ ] posture: env garbage → advisory + config fallback → default zero (EDGE-008)
- [ ] posture: env may not loosen committed config — floor stays floor (REQ-306)
- [ ] posture: prod-touching change with any degraded review → promotion refused (REQ-209)

## TDD Unit Test Map

| Source file | Test file | Functions/behaviors |
|-------------|-----------|---------------------|
| skills/land-queue/scripts/collect-queue.py | tests/test_collect_queue.py | union, dedupe, ordering, precheck terminals |
| scripts/gsd/takeover-check.sh | tests/bats/takeover-check.bats | 9 invariants, OK path, 72h, --rearm boundary |
| scripts/collect-status-facts.sh (TAKEOVER block) | tests/bats/takeover-check.bats (record-factory cases) | schema keys, forbid[] determinism |
| scripts/gsd/queue-guard.sh | tests/bats/queue-guard.bats | caps, sigs, breaker, infra-error distinctness |
| skills/land-queue/SKILL.md exec blocks | tests/bats/land-queue.bats | terminal states, resume, ci-timeout, continue-on-red |
| skills/git-branch-consolidate/SKILL.md 4-A | tests/bats/consolidate-autonomous.bats | conjunction, delta, refusals, finalizer delegation |

## Integration Tests

- INT-001: PATH-001 round trip — spec-status stub-run writes record → wall OK →
  feature-implement Step-2 gate passes (stubbed gates.py/git).
- INT-002: PATH-003 — 2-item queue over stubbed feature-implement/gh/finalizer →
  LANDED,LANDED, append-only json, empty Human inbox.
- INT-003: PATH-004 failure weather — BLOCKED continues, systemic aborts, resume
  skips LANDED.
- INT-004: PATH-005 consolidate matrix over stubbed estate/assert-merged.
- INT-005: posture floor vs zero on the same missing-reviewer fixture → the two
  divergent verdicts (REQ-209).

## Phase Test Gates

Gates are CUMULATIVE — each phase re-runs every earlier phase's suite.

| Phase | Gate condition | Command |
|-------|----------------|---------|
| Phase 1 | takeover suite green | `bats tests/bats/takeover-check.bats` |
| Phase 2 | + queue logic + guards | `python3 -m pytest tests/test_collect_queue.py -q && bats tests/bats/takeover-check.bats tests/bats/queue-guard.bats tests/bats/land-queue.bats` |
| Phase 3 | + consolidate + posture + lints | `bats tests/bats/takeover-check.bats tests/bats/queue-guard.bats tests/bats/land-queue.bats tests/bats/consolidate-autonomous.bats && python3 -m pytest tests/test_collect_queue.py -q && python3 scripts/verify-skill-blocks.py && python3 scripts/lint_host_dispatch.py skills/*/SKILL.md && python3 scripts/lint_model_routing.py` |
| Final | full suites vs baseline | `python3 -m pytest tests/ lib/ -q` (baseline 577 after PR #93) + full bats exit 0, main tree only — exclude `.worktrees/` and `.claude/worktrees/` from suite discovery (record the ok-count on main before Phase 1 starts; diff against it) |

Test substrate: bats fixtures use REAL local git (bare origin + worktrees —
run-finalizer.bats convention); only vendors (gh, codex/claude CLIs, the
implementer) are stubbed. The first production `/land-queue` run (program plan
step 5) is the live acceptance evidence.

Zero live vendor calls anywhere (AC-D03) — codex-output-schema-contract.bats
stays the only opt-in live test in the repo.

## Ledger separation
`--rearm` and `consolidate:estate` are the only new grant-minting paths; both
derive from deterministic artifact intersections (REQ-104/305). Everything else
consumes existing grants (`merge:pr`) or records `pending`. ASSUME lines and
scout prose never touch the ledger.

## Premise challenge (CEO step 0A)

- P-1 "N finished items pile up before main" — MEASURED, not assumed: 8+ stranded
  single-fix branches exist right now (`git branch -a`), incl. work that sat 2 days
  (PR #88) and cost a week of codex quota. The first production queue run IS the
  instrumentation (its report records queue size + terminal states).
- P-2 "guards of this kind work" — partially proven: the spec-005 19-round burn
  happened with NO cap; the cap landed in PR #91 and its quarantine semantics
  (distinct verdict, not BLOCKED) are what this plan's queue guards copy. Unproven
  half: caps at queue scope. Mitigation: REQ-204..207 test every guard; first live
  run is the acceptance gate.
- P-3 "operator wants zero-human default" — operator-LOCKED decision, not a model
  premise (recorded in program plan; floor flag preserves the alternative).
- P-4 "takeover record can be trusted" — explicitly REJECTED as a premise: the
  record is an index, never truth; the wall re-verifies live (risks #3).

## What already exists (CEO step 0B — verified in code this session)

| Sub-problem | Existing code | Verified |
|---|---|---|
| Estate enumeration w/ landed + dispositions | collect-estate.py (`landed`:292, dispositions:168-175, prints JSON by default — NOTE: no `--json` flag exists; drop the flag from invocations) | grep 2026-08-07 |
| File-overlap ordering input | NOT available from estate records — `files_changed` is a COUNT (:269); collect-queue.py must compute file lists via `git diff --name-only base...branch` itself | grep 2026-08-07 |
| Single-flight | scripts/gsd/liveness-check.sh | ls |
| Bounded exec (CI wait) | run_bounded in adversary-host.sh (:16-32) | grep |
| Round counters w/ infra-error distinctness | gates.py loop_round/_loops_ns (:374-418) | grep |
| Merge proof / finalize | assert-merged.sh, run-finalizer.sh | ls |

## NOT in scope (deferred with rationale)

- Token meter / spend dashboard — nothing exposes a usable total; wall-clock ×
  rounds is the honest proxy (named non-goal, risks #7).
- HMAC-signing takeover records — provenance defense is instead: wall re-verifies
  everything live + queue items must map to a real branch/PR + every merge still
  requires its own ledger grant; a forged record cannot mint grants (REQ-104
  boundary). Revisit in spec 008 (G9 fencing) if evidence demands.
- Handoff-doc untrusted-data fencing (G9), degradation counting (G4), digest
  (G12) — spec 008.
- Editing consumer repos' workflows, infra provisioning — spec 007 scope.

## Dual voices — consensus (/autoplan, 2026-08-07)

Voices: Codex (gpt-5.6, read-only exec, combined CEO+Eng+tests), Claude Opus
CEO subagent, Claude Opus Eng subagent, Claude Sonnet DX subagent — all fresh
context, artifact-only. DX ran subagent-only (codex quota economy after the
week-limit burn incident; tagged [subagent-only]).

| Dimension | Claude | Codex | Consensus |
|---|---|---|---|
| Premises valid? | challenged (base rate) | challenged (guards unproven) | DISAGREE → premise challenge section added; operator-locked direction stands |
| Right problem? | reframe: land-per-run | reframe: provider merge queue | DISAGREE → USER CHALLENGE, default stands (see audit #1) |
| Architecture sound? | shape yes, 3 reuse claims false | no durable state machine | PARTIAL → C1/C2/H1 + journal/reconcile adopted |
| Test coverage sufficient? | gaps (M4) | gaps (false-green table) | CONFIRMED-GAP → 20 cases added to Unit Test List |
| Security/authority boundaries? | --rearm hole (C2) | --rearm + consolidate:estate circular | CONFIRMED → REQ-104 store-history derivation; REQ-305 scope tests |
| Deletion risk manageable? | conjunction ok | set-equality + CAS needed | CONFIRMED → REQ-302 set tuples + report-only default |
| Posture default | floor default (challenge) | env must not weaken | SPLIT → zero default stands (operator-locked); strictness ordering adopted (REQ-306) |

## Decision Audit Trail (/autoplan, 2026-08-07, autonomous — operator pre-authorized full autonomy)

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Keep /land-queue + FFS-native landing vs provider merge queue / land-per-run reframe | USER CHALLENGE — surfaced, default stands | — | Operator-locked program: queue must drive /feature-implement per item under the grant ledger; bors/homu/native queue own the merge decision themselves (prior-art.md) | GitHub native queue; kill Phase 2 |
| 2 | CEO | posture zero stays DEFAULT | USER CHALLENGE — surfaced, default stands | — | Operator explicitly locked "absolute zero default with floor flag"; strictness-ordered env override (REQ-306) narrows the abuse case both voices raised | floor-as-default ratchet |
| 3 | CEO | STOP/drain kill switch + revert SHA per LANDED | Auto | P1 | No abort/undo path existed; 8h unattended queue needs an operator brake | none |
| 4 | CEO | Queue-level single-flight lock (REQ-212) | Auto | P4 reuse (_StoreLock family) | Two concurrent queues were unguarded | liveness-check reuse (self-deadlock, Eng C1) |
| 5 | CEO | max-items stays 10 | Taste — logged, kept | P6 | First production run must drain ~8 stranded branches; caps 204-207 bound the risk | max-items 3 v1 |
| 6 | Eng | Wall single-flight = O_EXCL lockfile, drop liveness-check | Auto | P5 | liveness-check is a detector, not a mutex; its ship-grant signal self-deadlocks queue items (verified in code) | rewrite liveness-check |
| 7 | Eng | --rearm derives from store grant HISTORY, never branch files | Auto | P1 (security) | record ∩ tasks.md are both attacker-controlled on the taken-over branch — grant minting hole (both voices) | HMAC signing (over-scale; deferred 008) |
| 8 | Eng | <store> = directory via new gates.py store-dir accessor | Auto | P5 | _store_path returns the evidence.json FILE; every <store>/takeover path was unimplementable as written | per-script re-derivation |
| 9 | Eng | REQ-303 refined: refused = force-delete WITHOUT merged-head proof; executed-refusal tests | Auto | P5 | run-finalizer's -D-under-proof is sanctioned; the old wording contradicted REQ-304; grep tests are vacuous | — |
| 10 | Eng | Staleness = min(created_at, mtime) + store granted_at cross-check | Auto | P1 | self-reported timestamp on an untrusted record | trusting record field |
| 11 | Eng | Intake bounded + queue clock from intake; disposition filter (not landed); fetch first; file lists computed by collect-queue | Auto | P1 | intake ran outside every guard; landed==false drops docs-only branches; files_changed is a count (all verified) | extending collect-estate.py |
| 12 | Eng | Item journal + resume reconciliation via assert-merged (REQ-208/213) | Auto | P1 | crash between merge and LANDED append corrupts queue truth (both voices, codex "2am path") | SQLite/event-store (over-scale) |
| 13 | Eng | EDGE-006 fail-closed QUEUE-ERROR:store, divergence from loop-round fail-open documented | Auto | P5 | continuing without journal writes risks double-merge on resume; loop-round's fail-open protects a different invariant | fail-open mirror |
| 14 | Eng | Consolidate proof = per-target SET tuples + ref-unmoved check; --report-only default first run | Auto | P1 | count equality admits equal-count/wrong-set substitution (codex); report-only is a one-flag safety ratchet, not a direction change | removing Phase 3 deletion (challenge — operator direction stands) |
| 15 | Eng | Circuit breaker trips only on enumerated systemic classes | Auto | P5 | string-normalized classes collide → false systemic aborts | full failure-code taxonomy |
| 16 | Eng | --parallel REFUSED in v1 (grammar reserved) | Taste — adopted | P3/P6 | both voices: worktree-path assert doesn't cover shared refs/ledger/provider races; design risk #5 already said cut-first | shipping --parallel 2 |
| 17 | Eng | branch-gone disambiguation: head-reachable → reconciled LANDED, else BLOCKED:source-missing | Auto | P1 | deleted-unmerged vs externally-merged are opposite outcomes, both flowed to skip | silent SKIPPED |
| 18 | Eng | identity = (branch, head SHA); conflicts → BLOCKED:identity-conflict | Auto | P5 | richest-record merge can synthesize a Frankenrecord | richest-record dedupe |
| 19 | DX | Every refusal prints one-command remedy; Human-inbox schema is a REQ tested in PATH-004 | Auto | P1 | headline promise had zero coverage; 7/9 refusals lacked remedies | — |
| 20 | DX | takeover-check --list for cold-session discovery | Auto | P1 | discovery path existed only in spec prose | new skill (over-scale) |
| 21 | DX | Rule 12 → explicit 12a/12b split; autonomy-posture.md ≤60 lines | Auto | P5 | silent rewrite under the old number breaks the 2-minute-read contract | bolt-on clause |
| 22 | DX | Verdict grammar documented once in SKILL.md (3 shapes incl. legacy BLOCKED: prose) | Auto | P5 | third colon-shape was undocumented | repo-wide rename (blast radius) |
| 23 | Eng | REQ-209 denominator = current run; any degraded review on prod-touching change blocks promote | Auto | P1 | >50% average could bury one critical degraded review | risk-class matrix (over-scale) |
