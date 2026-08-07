# Spec 006 — Autonomous landing (takeover record + /land-queue + consolidate 4-A + posture knob)

> Close the last human-shaped gaps between "work finished on a branch" and "work on
> origin/main": a machine-readable takeover record + deterministic re-verify wall so
> any session can safely take over another's run; a `/land-queue` skill that drains N
> finished items to origin/main with the guards that the spec-005 (19-round wall loop)
> and codex (38-turn burn) incidents proved must ship WITH the loop; an autonomous
> Step 4-A for `git-branch-consolidate`; and the operator-locked autonomy posture knob
> (`zero` default, `floor` flag).

## Context

- Program contract: `/Users/luminamao/.claude/plans/humble-percolating-clock.md`
  (approved). Design source: `.planning/prior-art/spec-006-autonomous-landing.md`
  (opus design fan-out + red-team, 2026-08-07). REQ tables therein are carried
  verbatim as this spec's acceptance criteria.
- Operator decisions LOCKED: autonomy posture `zero` is DEFAULT with a `floor`
  flag restoring the 4-point human floor; docs must specify both. G1 credential
  hardening declined — accepted risk (documented in docs/promotion-protocol.md).
- Landed prerequisites (main @ c133bad): wall round cap (`gates.py loop-round`,
  `WALL-ROUND-CAP` quarantine verdict), tamper CI job, `_store_path` pinned via
  `git rev-parse --git-common-dir` (worktrees resolve to the MAIN checkout's
  store), codex reviewer fixed (object-root schema, live cross-vendor proof).
- Takeover is NOT a new skill: `/spec-status` (v1.2.0) emits the record; a new
  deterministic wall script gates takeover; ONE line wires it into
  `skills/feature-implement/SKILL.md` Step 2. No `/takeover-resume`, no
  `--from-handoff` — a flag a cold session forgets is not a wall.
- State lives beside the evidence store: `<store>/takeover/<run-id>.json` (+ `.md`
  prose superset) and `<store>/land-queue/<queue-id>.json` — inherits the
  decoy-store defense and worktree/host neutrality. `.planning/` is gitignored AND
  per-worktree — never a discovery path. Discovery = glob `<store>/takeover/*.json`.
- Reuse: `skills/git-branch-consolidate/scripts/collect-estate.py` wholesale for
  queue intake; `scripts/gsd/liveness-check.sh` for single-flight;
  `run-finalizer.sh` merged-head proof for all branch deletion.

## Non-negotiable autonomy constraints (operator-mandated)

- Grants are exact typed entries walked from deterministic artifacts. The ONE
  NEW auto-grant path this spec adds (`consolidate:estate`) derives from the
  enumerated queue — never from prose, never from scout output (MAX-AUTH
  derivation boundary). (`--rearm` was descoped 2026-08-07 — takeover never
  auto-re-issues grants; refusal + manual re-grant remedy only.)
- Guards ship WITH the loop, in the same phase — round cap 2/item, item wall-clock
  90m, queue wall-clock 8h, max-items 10, no-progress signatures, circuit breaker.
  The parallel lane is cut before any guard is.
- ITEM terminal states are exactly three: `LANDED` | `BLOCKED:<reason>` |
  `SKIPPED:<reason>`. Distinct-scope verdicts (documented once in
  land-queue/SKILL.md as the complete verdict grammar): wall-level
  `TAKEOVER-OK`/`TAKEOVER-REFUSED:<reason>`, queue-level
  `QUEUE-ABORTED:systemic:<class>` / `QUEUE-ABORTED:operator-stop` /
  `QUEUE-REFUSED:queue-live`. (The colon-token shape is deliberate machine
  grammar; the repo's older `BLOCKED: <prose>` review-gate shape is a different
  surface — the SKILL.md notes the split once.) No token meter (nothing exposes
  a usable total; wall-clock × rounds is the honest proxy — SKILL.md says so
  explicitly).
- Refused under EVERY grant, both postures: force-deletes, dirty-worktree deletes
  (route to `/adopt-wip`), unlanded deletes, force-push to base, `git add -A`.

## User Stories

### US-1: Takeover record + re-verify wall (Phase 1)
As a session taking over another session's run, I get a machine-readable takeover
record written by `/spec-status`, and a <5s deterministic wall
(`takeover-check.sh`) that re-verifies every invariant LIVE before I may act —
so a stale or poisoned record can never make me merge, rerun, or discard work
unsafely.

### US-2: /land-queue (Phase 2)
As the operator, I run `/land-queue` and N finished items (takeover records,
estate branches, explicit specs) drain to origin/main — each item rebased,
finished, CI-waited (bounded), merged under grant, finalized — with caps and a
circuit breaker so one bad item or a systemic failure never becomes another
week-long burn. The report ends with a Human inbox of one-command unblocks.

### US-3: Autonomous consolidate + posture knob (Phase 3)
As the loop, after a queue lands work I consolidate the branch estate under a
4-part machine-evidence conjunction (never a vibe), and the repo carries
`autonomy.posture: "zero"|"floor"` — zero (default) keeps humans async
observers; floor restores the 4-point human floor. Both are documented.

## BDD Scenarios

Feature: Autonomous landing

Scenario: spec-status writes a takeover record beside the evidence store
  Given a spec run with a resolvable evidence store and grant ledger
  When  /spec-status runs for that spec
  Then  <store>/takeover/<run-id>.json exists with ids, git state, grants,
        pendings, forbid[] and resume fields — and a .md prose superset beside it

Scenario: takeover wall refuses a decoy store
  Given a takeover record whose gates_store realpath differs from the resolved store
  When  takeover-check.sh runs for that run-id
  Then  it exits nonzero printing TAKEOVER-REFUSED:decoy-store before any ledger read

Scenario: takeover wall passes a clean handoff
  Given a takeover record whose 9 invariants all hold live
  When  takeover-check.sh runs
  Then  it prints TAKEOVER-OK and exits 0 in under 5 seconds

Scenario: land-queue drains a finished branch
  Given a takeover record for a finished spec branch with fresh preflight and grants
  When  /land-queue runs
  Then  the item is rebased, finished, CI-waited, merged under its merge:pr grant,
        finalized, and reported LANDED

Scenario: one red item does not stop the queue
  Given a queue of three items where the second fails its gate twice
  When  /land-queue runs
  Then  item two reports BLOCKED:no-progress and items one and three still reach
        their own terminal states

Scenario: systemic failure aborts the queue
  Given two consecutive items blocking with the same failure class
  When  the second block is recorded
  Then  the queue stops with QUEUE-ABORTED:systemic:<class> and the report lists
        every unprocessed item as SKIPPED:queue-aborted

Scenario: CI timeout never merges
  Given an item whose PR checks are still pending at the 20-minute bound
  When  the bounded CI wait expires (rc 124)
  Then  the item reports BLOCKED:ci-timeout and no merge is attempted

Scenario: autonomous consolidate refuses without full evidence
  Given a consolidate run missing any one of grant, fresh estate, delta match, or
        assert-merged proof
  When  git-branch-consolidate Step 4-A --autonomous runs
  Then  it refuses with exit 1 naming the missing evidence item

Scenario: zero posture degrades privilege, not throughput
  Given autonomy.posture is zero and >50% of a run's reviews were same-vendor degraded
  When  the run attempts promotion to prod
  Then  promotion is refused while merge remains permitted, and the degradation
        count appears in the report

Scenario: floor posture keeps the human floor
  Given autonomy.posture is floor and no cross-vendor reviewer is reachable
  When  a queue item reaches its review step
  Then  the item reports BLOCKED:no-cross-vendor-reviewer and no same-host review
        substitutes

## Acceptance Criteria

Carried verbatim from the design REQ tables (`.planning/prior-art/spec-006-autonomous-landing.md`).

### Phase 1 — takeover record + wall
- REQ-101: `/spec-status` writes `<store>/takeover/<run-id>.json`; a bats fixture
  asserts the schema keys (ids, gates_store realpath, branch/head/upstream,
  preflight, grants w/ TTL + rearm cmds, pendings, promotions, runner liveness,
  git_state, unresolved HIGH/CRITICAL findings, phases, evidence pointers,
  forbid[], resume{command,preconditions}).
- REQ-102: `forbid[]` entries come ONLY from deterministic probes — scout stubs
  emitting poison-list prose produce ZERO forbid entries.
- REQ-103: `takeover-check.sh` exits 1 with the correct
  `TAKEOVER-REFUSED:{decoy-store|runner-live|mid-rebase|branch-gone|dirty-worktree|preflight-stale|grant-expired|findings-open|poison/<action>}`
  per violated invariant — one bats case per invariant, stubbed git/gh/gates.py.
  Single-flight (`runner-live`) uses an `O_EXCL` lockfile beside the evidence
  store (same serialization family as gates.py `_StoreLock`), NOT
  liveness-check.sh — liveness-check is an OR-of-three detector whose grant
  signal (`check-grant --action ship:gsd`) returns ALIVE for the queue's own
  in-flight run and would self-deadlock every item; a bats case pins that the
  wall passes while the item's own ship grant is live. EVERY refusal line
  prints a one-command remedy beside its reason (plan-wall's
  "Unblock (operator):" bar) — bats asserts the remedy per invariant.
  `takeover-check.sh --list` enumerates discoverable records (run-id, age,
  branch, resume command) for cold sessions; referenced from
  feature-implement's SKILL.md.
- REQ-104: expired or missing grants are NEVER auto-re-issued (descoped
  2026-08-07: `--rearm` removed from v1 — three wall rounds plus a post-reset
  round kept finding TTL-anchor and self-renewal holes in every re-issue
  design; operator chose refusal-only). The wall refuses `grant-expired` /
  `preflight-stale` with a remedy line naming the exact manual re-grant /
  re-preflight command; the operator re-arms by running it. Re-issue may
  return as its own spec with a dedicated design round.
- REQ-105: `feature-implement --autonomous` fails closed on a wall refusal; with no
  takeover record present the wall is a no-op — BOTH halves get dedicated tests
  (the no-op half guards every existing feature-implement run).
- REQ-106: a GATES_STORE decoy mismatch is caught BEFORE any ledger read.
  `<store>` throughout this spec = the store DIRECTORY (`_store_path().parent`,
  i.e. `.feature-fix-swarm/`); a new `gates.py store-dir` accessor prints it so
  no shell script re-derives the path.

### Phase 2 — /land-queue
- REQ-201: `collect-queue.py` unions the 3 intake sources (takeover/*.json,
  collect-estate.py output filtered on landable `disposition` — NOT the `landed`
  boolean, which is false for docs-only branches the queue exists to drain —
  and explicit args), `git fetch` first, computes each item's file set itself
  via `git diff --name-only base...branch` (estate records carry only counts),
  and dedupes by (branch, head SHA). Sources that disagree on head SHA, run id,
  or spec id for one branch → `BLOCKED:identity-conflict`, never a merged
  "richest" record (pytest fixtures). Intake itself runs under `run_bounded`
  and starts the queue clock.
- REQ-202: overlap groups (file-set intersection, transitive — connected
  components) serialize oldest-first; disjoint singletons order by smallest
  residual file count (pytest, deterministic, incl. a transitive A∩B,B∩C,A∩C=∅
  case). v1 executes strictly serially.
- REQ-203: precheck emits terminal states with ZERO model calls, re-checked at
  ITEM START not just intake: already-landed → SKIPPED:already-landed;
  branch gone WITH its recorded head reachable from base → reconciled LANDED;
  branch gone WITHOUT that proof → `BLOCKED:source-missing` (Human inbox),
  never a silent skip; trial merge-tree conflict after one rebase attempt →
  BLOCKED:conflict.
- REQ-204: `queue-guard.sh` trips round cap (2/item), item wall-clock (90m), queue
  wall-clock (8h, measured from intake), and max-items (10) — bats.
- REQ-205: the same normalized failure signature twice → `BLOCKED:no-progress` via
  the existing note-failure mechanism (`<gate>|<first stderr line, digits/paths stripped>`).
- REQ-206: circuit breaker trips ONLY on an enumerated systemic class list
  (reviewer-unreachable, store-error, gh-auth, network) — 2 consecutive blocks
  in one class → `QUEUE-ABORTED:systemic:<class>`. Item-local defect classes
  (test-failure, conflict, review-findings) never trip it, however many items
  hit them (prevents string-collision false aborts).
- REQ-207: one red item does not stop the queue — later items still reach terminal
  states.
- REQ-208: `--resume <queue-id>` never re-executes a LANDED item, and RECONCILES
  every non-terminal item against authority (`assert-merged.sh` / `gh pr view`)
  before re-executing: a crash after merge but before the LANDED append must
  resume to reconciled-LANDED with NO second merge attempt (crash-injection
  bats: kill between merge and journal append).
- REQ-209: missing opposite-CLI reviewer → floor mode: `BLOCKED:no-cross-vendor-reviewer`,
  never same-host review; zero mode: counted degradation (note-degraded), merge
  permitted, prod promotion refused when degraded reviews exceed 50% of THIS
  run's reviews — and ANY degraded review on a prod-touching change refuses
  promotion regardless of ratio (no averaging away critical evidence).
- REQ-210: CI wait is bounded (`run_bounded 1200` — seconds, sourced from
  scripts/gsd/run-bounded.sh — over `gh pr checks`); rc 124 →
  `BLOCKED:ci-timeout` with NO merge attempted (hanging-gh stub test). Before
  the merge call the item re-reads the PR head SHA and refuses on drift
  (`BLOCKED:head-moved`); empty-check-suite, merge-conflict, and gh-auth-failure
  fixtures each produce their named BLOCKED reason, never a merge.
- REQ-211: `<store>/land-queue/STOP` is checked before every item and again
  before every merge — present → queue stops with `QUEUE-ABORTED:operator-stop`
  (current side effect completes, nothing new starts). `--drain` finishes the
  in-flight item then stops. Every LANDED entry records its merge commit SHA
  (one-command `git revert` from the report).
- REQ-212: queue-level single-flight — an `O_EXCL` lockfile beside the store;
  a second `/land-queue` on the same store refuses immediately
  (`QUEUE-REFUSED:queue-live`), stale-lock takeover only via dead-pid proof.
- REQ-213: append-only per-item journal in the queue json: intent (branch, head
  SHA, PR#, action) written BEFORE each side effect, observed result after —
  resume reconciliation (REQ-208) reads it; malformed/hostile records (wrong
  types, path traversal in store paths, symlinked record, oversized file) are
  refused by schema validation with named errors, never partially consumed.
- REQ-214: every non-LANDED terminal carries `reason` + a one-command unblock in
  the report's Human inbox (schema pinned by bats in the PATH-004 unhappy path);
  `--parallel` is REFUSED in v1 with `PARALLEL-UNSUPPORTED:v1-serial-only`
  (races on shared refs/ledger unproven — grammar reserved, not implemented).

### Phase 3 — consolidate 4-A + posture
- REQ-301: Step 4-A requires all four evidence items (consolidate:estate grant +
  fresh estate landed==true per target + exact target-set proof (REQ-302) +
  assert-merged green per PR); any absent → refuse (one bats per missing item).
  First production run defaults to `--report-only` (prints the deletion plan
  with evidence, deletes nothing); executing deletion requires the explicit
  `--execute` flag on top of the conjunction.
- REQ-302: SET equality per target, not count equality — each deletion target
  carries the tuple (branch ref, expected tip OID, PR#, observed merge commit);
  a swapped target with a matching count, a duplicate target, or a tip that
  moved after proof each → exit 1 with the named mismatch (bats: equal-count/
  wrong-set substitution case, branch-advanced-before-delete case).
- REQ-303: force-delete WITHOUT merged-head proof / dirty-worktree delete /
  unlanded delete / force-push to base / `git add -A` are refused under EVERY
  grant — asserted by EXECUTED refusals in bats (a stubbed run showing the
  refusal path fire), not by grepping SKILL.md prose. (run-finalizer's
  `-D`-under-merged-head-proof is the one sanctioned force-delete form.)
- REQ-304: deletion routes through run-finalizer's merged-head proof — no second
  deletion path; finalizer refuses when the ref no longer equals the proven
  OID (drift → report, never delete).
- REQ-305: `consolidate:estate` grant is derived from the enumerated queue with
  TTL ≤ queue-timeout; scope-substitution and expiry-between-check-and-effect
  each have a refusal test.
- REQ-306: posture resolution — committed config default `"zero"`; the
  `FFS_AUTONOMY_POSTURE` env override may only make policy STRICTER
  (`floor` accepted; an env demanding `zero` over a `floor` config is ignored
  with a stderr advisory — an agent-controlled env var must never weaken
  policy); garbage values → advisory + fall through. Effective posture +
  provenance are printed once per run. Phase 3 enumerates every call site that
  reads the posture (the 4-point-floor consumers) — the knob without its
  consumers is a no-op.

### Cross-phase
- AC-D01: `docs/autonomy-posture.md` exists (≤60 lines) specifying BOTH postures
  (zero default, floor flag), the degraded-review promotion rule (REQ-209),
  quarantine auto-requeue-once, and the accepted credential risk;
  `docs/promotion-protocol.md` Rule 12 is explicitly SPLIT into 12a (emergency
  bypass, now posture-dependent) and 12b (grant-registry maintenance,
  registering `consolidate:estate`) with the doc's rule-count line updated —
  never a silent rewrite under the old number.
- AC-D02: every new lever ships a `bash verify` block (`test -f`) and
  `verify-skill-blocks.py`, `lint_host_dispatch.py`, `lint_model_routing.py` stay
  green.
- AC-D03: zero live vendor calls in the test suites — all bats/pytest run against
  stubs (adversary-host.bats / assert-merged.bats / run-finalizer.bats conventions).

## E2E Test Paths

CLI/orchestration feature — E2E = bats scenarios over stubbed git/gh/CLIs (no
browser surface; Playwright N/A by construction).

- PATH-001: spec-status → takeover record → takeover-check TAKEOVER-OK →
  feature-implement Step-2 wall passes (full handoff round trip, stubs).
- PATH-002: takeover-check refusal matrix — all 9 invariants individually violated,
  each yielding its named TAKEOVER-REFUSED reason (REQ-103).
- PATH-003: land-queue happy path — 2-item queue drains to LANDED,LANDED with
  append-only queue json and Human-inbox-empty report.
- PATH-004: land-queue failure weather — red item → BLOCKED, queue continues;
  same-class twice → QUEUE-ABORTED:systemic; --resume skips LANDED.
- PATH-005: consolidate 4-A — full conjunction executes deletions via
  run-finalizer proof; each missing evidence item refuses.

## Edge Cases

- EDGE-001: takeover record older than 72h → hard refusal (staleness ≥ grant TTL),
  even if every other invariant holds. Age = `now − min(record.created_at,
  file mtime)`, cross-checked against the store's `granted_at` for the run —
  a poisoned record cannot self-report freshness.
- EDGE-002: record's branch deleted upstream → TAKEOVER-REFUSED:branch-gone, not a
  crash. The probe is LOCAL git only (refs fetched at intake); offline →
  refusal names the network cause distinctly.
- EDGE-003: dirty-set GREW since record (new untracked files) →
  TAKEOVER-REFUSED:dirty-worktree routing to /adopt-wip; IDENTICAL dirty-set
  passes (a WIP handoff is the wall's normal case — dedicated pass-case test).
  Context split, stated in both SKILL.mds: the takeover WALL tolerates
  recorded WIP; the QUEUE requires clean worktrees it created itself.
- EDGE-004: two sessions run takeover-check concurrently → single-flight via the
  O_EXCL lockfile (REQ-103); second gets TAKEOVER-REFUSED:runner-live
  (concurrency bats: two interleaved walls, exactly one proceeds).
- EDGE-005: queue item whose branch merges externally mid-queue → item-start
  recheck reports it per REQ-203 (reconciled LANDED with head proof, else
  BLOCKED:source-missing), not a rebase failure — tested at item start, not
  only intake.
- EDGE-006: corrupt/unwritable queue json → queue aborts with
  `QUEUE-ERROR:store` — an infra verdict deliberately DISTINCT from both item
  failures and `QUEUE-ABORTED:systemic` (fail-closed here, unlike loop-round's
  fail-open, because continuing without journal writes risks double-merge on
  resume; the divergence is documented at the code site).
- EDGE-007: `--parallel` (any value) → `PARALLEL-UNSUPPORTED:v1-serial-only`
  (REQ-214); the flag parses, the refusal is tested, no lane launches.
- EDGE-008: posture env override `FFS_AUTONOMY_POSTURE` set to garbage → treated
  as unset (config value, then default zero) with one stderr advisory; env
  demanding looser-than-config → ignored with advisory (REQ-306 strictness
  ordering, both cases tested).
- EDGE-009: preflight-stale refusal prints the exact `/preflight` re-run
  command as its remedy; any secret named in refusal output is BY NAME only,
  never a value. (Was: rearm auto-re-runs preflight — descoped with --rearm.)
- EDGE-010: quarantined item auto-requeues ONCE after base advances (zero mode);
  second quarantine parks permanently — no infinite requeue. Implemented as a
  tested state transition in Phase 2 (requeue counter in the queue json), not
  documentation-only.

## E2E Test Stubs

```bash
# tests/bats/takeover-check.bats
# stubs: git (rev-parse/status/branch), gh, gates.py (check-preflight/check-grant),
# liveness-check.sh. One case per invariant (REQ-103) + OK path + 72h staleness.

# tests/bats/queue-guard.bats
# counters in a temp store; cases: round cap, item clock, queue clock, max-items,
# no-progress sig, circuit breaker, corrupt-store distinct error (EDGE-006).

# tests/bats/land-queue.bats
# stubbed collect-queue/feature-implement/gh/assert-merged/run-finalizer;
# PATH-003/004 flows, --resume semantics, terminal-state exactness.

# tests/bats/consolidate-autonomous.bats
# 4-part conjunction matrix (REQ-301), delta mismatch (REQ-302),
# refused-action matrix (REQ-303), finalizer-delegation grep (REQ-304).

# tests/test_collect_queue.py
# union/dedupe (REQ-201), ordering (REQ-202), precheck terminals (REQ-203).
```

## Test Contract Summary

| Layer                  | Count | Status  |
|------------------------|-------|---------|
| BDD Scenarios          | 10    | draft   |
| Acceptance criteria    | 26 REQ + 3 AC-D | listed (REQ-101..106, 201..214, 301..306) |
| Unit test cases        | 45+ (plan.md Unit Test List) | listed |
| Integration tests      | 5 INT (plan.md) | defined |
| E2E paths (bats)       | 5     | stubbed |
| Edge cases             | 10    | listed  |
