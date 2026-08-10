# Spec 010 — Run-liveness healing: durable run lifecycle (waiting/wake-condition state machine) + reconciler

**Status:** draft v2 (v1's five bespoke mechanisms reframed at the /autoplan gate — operator
chose the state-machine architecture both reviewer voices recommended)
**Branch:** `010-run-liveness-healing`
**Depends on:** spec-004 (plan wall), spec-008 (run-status file, budget accounting), spec-009 (coordination store)
**Driver evidence:** spec-005 forensics (`.planning/HANDOFF-init-heal-retro-20260810.md`) — 53h46m
wall-clock, ~7h14m active (13.5%). Ranked stall classes: ~29h turn-ended-on-background-wall
(untouched), ~4-5h wall re-litigation (already fixed, PR #91/#96), 2h49 CI outage hand-holding,
72m session limit. Model tier was not a driver. **Evidence honesty: this ranking is n=1**
(one run's forensics) — it justifies fixing these incident classes, not treating the hour
figures as stable rates; spec-011's retro instruments the metric that will give n>1.

## Context

FFS autonomous runs lose most of their wall-clock to *sleep*, not work: a background
plan-wall outlives the turn that owned it; a dead executor is quarantined but never
revived; a session limit pauses a run with no scheduled wake; CI failures get hand-rolled
polling. v1 of this spec proposed five bespoke bash mechanisms. The /autoplan review
(two independent judgment-tier voices + operator decision) reframed them as what they are:
**four wait-types of one lifecycle** —

```
running → waiting(reason, wake_condition) → runnable → running → done|failed|quarantined
```

A worker that hits a wait condition CHECKPOINTS durable state and EXITS with a structured
outcome (releasing its locks) instead of sleeping in-process or dying silently. A one-shot
**reconciler** verb evaluates wake conditions and relaunches runnable work under durable,
run-wide budgets. In-process recovery that dies with the process tree is exactly what the
spec-005 incident disproved; durable state + external scheduling is the fix that also
absorbs the NEXT wait-type without a sixth script.

A fifth, preventive item is unchanged from v1: a plan-length gate at decompose
(≤300-line plans converged at the wall in 2 rounds vs 8 — confounded n=2 evidence, noted
honestly, enforcement kept hard per operator decision).

## Scope boundary (operator-locked, revised at gate)

- **No long-lived daemon SHIPPED.** The reconciler is a one-shot idempotent verb. Continuous
  scheduling comes from (a) opportunistic reconcile passes at FFS entrypoint skills (the
  init-gate advisory precedent) and (b) an operator-installed cron/launchd line documented
  in `docs/healing.md`. FFS never installs a crontab itself.
- Every wait/retry has a durable run-wide budget (`*_MAX*` env), every budget-hit is a
  typed exit; counters live in the lifecycle record — a chain of resumes cannot reset them.
- Kill-switch convention `<FEATURE>=off`: `FFS_LIFECYCLE=off` disables checkpoint-and-exit
  (today's behavior); `FFS_RECONCILE=off` makes reconcile a typed no-op.
- Healing never overrides a gate verdict: wake-condition evaluation *reads* wall state, it
  never flips a verdict; a `quarantined` run (budget breach, wall round cap) is NEVER
  auto-relaunched.
- No new runtime dependencies (bash + python3 + gh + jq only).
- Untouched: `lib/ffs_installer.py`, `docs/installer.md`, `tests/test_installer.py`
  (another session's live files; installer's `scripts/gsd/*.sh` glob ships new scripts
  automatically — verified).

## Architecture summary

| # | Component | Mechanism |
|---|---|---|
| 1 | Lifecycle store — `scripts/gsd/lifecycle.sh` | One JSON record per run under `.planning/run-state/lifecycle-<run>.json`, written atomically (tmp+mv). Schema: `run_id, state ∈ {running, waiting, runnable, done, failed, quarantined}, reason, wake_condition {type ∈ wall-decided|time|ci-completed|manual, params}, resume_argv, budgets {respawns, wakes, ci_reruns}, waiting_since, wake_at, updated_at`. Verbs: `checkpoint | transition | show | validate`. Illegal transitions are typed rc-1 errors. `LIFECYCLE:` typed lines. |
| 2 | Wake-condition evaluators | `wall-decided`: `plan-wall.sh --await <secs>` (new verb — bounded poll of the phase's durable verdict records; also callable inline by skills). `time`: reached when `now ≥ wake_at`; `wake_at` comes from the session-limit banner parse (tail-50-lines scope, strict `HH:MM` validation before arithmetic, rollover to tomorrow, cap `FFS_SESSION_WAKE_MAX_SECS=21600`). `ci-completed`: one bounded `gh run view` on a PINNED `databaseId` with attempt-gated classification (infra keyword table → `gh run rerun --failed` under the durable `ci_reruns ≤ 2` budget; test failure → terminal). |
| 3 | Reconciler — `scripts/gsd/reconcile.sh` | One idempotent pass, never a loop: for each `waiting` record, evaluate its wake condition (bounded, seconds not hours); satisfied → transition `runnable` → relaunch `resume_argv` (same host, budgets decremented durably) → `RECONCILE:relaunched`; unsatisfied → leave untouched, `RECONCILE:still-waiting`; `quarantined|done|failed` → never touched. Concurrency-safe via the spec-009 coord claim on the run key. Scheduling: entrypoint-skill opportunistic pass + documented operator cron line. |
| 4 | Worker wiring — `gsd-run.sh` | (a) Post-drive respawn-or-checkpoint: rc 124 → respawn once regardless of commits (idempotent resume); other nonzero rc + zero commits (git probe fails CLOSED) → respawn once; `quarantined` status → never respawn; budget durable (`FFS_RESPAWN_MAX=1`); attempt 2 appends to the log (`>>` + procsub wait) with remaining-time budget. (b) Session-limit banner in the drive capture tail → checkpoint `waiting(time, wake_at)` and EXIT (locks released, `sleeping` never holds the repo) instead of in-process sleep. (c) Wall pending at the pre-execution seam → checkpoint `waiting(wall-decided)` and exit rc 75. |
| 5 | Plan-length gate — `scripts/gsd/plan-length-gate.sh` | Unchanged from v1: `<PHASE_DIR|PLAN_FILE>`, every plan over `FFS_PLAN_MAX_LINES` (default 300) listed as `PLAN-LENGTH:<file>:<lines>:<limit>`, rc 1 (all violations, not first); rc 0 silent when compliant; empty phase dir rc 1 typed. Required by `skills/spec-decompose/SKILL.md` before the coherence gate; over-limit → replan, never truncation. |

Skill wiring: `skills/feature-implement/SKILL.md` gains `## Wall await rule` — the wall
stays FOREGROUND for 1-2-plan phases (the documented SKILL.md:240 contract); phases that
can exceed the 600s tool ceiling (≥3 plans × `PLAN_WALL_TIMEOUT=180s` + rounds) MAY
background it but MUST then loop `plan-wall.sh --await 300` (≤ `PLAN_WALL_AWAIT_MAX=6`
iterations) in the same turn, and if the turn cannot outlast the wall it MUST checkpoint
`waiting(wall-decided)` via `lifecycle.sh` before ending — a turn never ends wall-pending
without a durable waiting record. Entrypoint skills (the 7 with Init gates) run one
opportunistic `reconcile.sh` pass beside the init guard (advisory, fail-soft).

## User stories

- **US1 (orchestrator):** after launching a background wall task I either await it to a
  decision in-turn or checkpoint a durable waiting record — the run can always be resumed
  by the reconciler, recovering the ~29h/incident idle class.
- **US2 (operator):** a dead executor (timeout or zero-commit failure) is respawned once
  under a durable budget with typed lines; a budget-quarantined run is never auto-revived.
- **US3 (operator):** a session-limited run checkpoints its reset time and exits cleanly;
  the next reconcile pass (cron or any FFS entrypoint) relaunches it after the reset —
  no process sleeps for hours holding locks.
- **US4 (orchestrator):** CI waits are lifecycle records too: the reconciler classifies a
  finished run, reruns infra flakes under a durable ≤2 budget, and surfaces test failures
  immediately.
- **US5 (planner):** spec-decompose refuses any plan over 300 lines, at the point where
  replanning is cheap.

## BDD Scenarios

Feature: durable run lifecycle with bounded reconciliation

Scenario: await verb returns done when the wall passes (US1 happy)
  Given a phase directory whose every plan has a PASS-class wall verdict record for this run
  When the orchestrator runs plan-wall.sh --await 300 on that phase directory
  Then the command exits 0 within one poll interval and prints a typed WALL-AWAIT:done line

Scenario: a decided-but-blocked wall is never reported as pass (US1 control contract)
  Given a phase directory where every plan is terminal but one verdict is WALL-ROUND-CAP
  When the orchestrator runs plan-wall.sh --await 300 on that phase directory
  Then the command exits 20 with a typed WALL-AWAIT:decided-blocked line

Scenario: stale or foreign verdict records do not satisfy the wall (US1 trust)
  Given a verdict record whose plan_sha256 no longer matches the plan file on disk
  When plan-wall.sh --await 2 evaluates that phase
  Then the record counts as undecided and the command exits 75 pending

Scenario: a wall the turn cannot outlast becomes a durable waiting record (US1 checkpoint)
  Given a background wall still undecided at the await iteration cap
  When the orchestrator checkpoints the run via lifecycle.sh
  Then the lifecycle record holds state waiting with wake_condition wall-decided and the resume argv

Scenario: the reconciler relaunches a decided run (US1/US3 happy)
  Given a lifecycle record in state waiting whose wake condition is now satisfied
  When reconcile.sh runs one pass
  Then the record transitions through runnable, the resume argv is relaunched, and a RECONCILE:relaunched line is emitted

Scenario: the reconciler never revives a quarantined run (US2 safety)
  Given a lifecycle record in state quarantined
  When reconcile.sh runs one pass
  Then the record is untouched and no relaunch occurs

Scenario: dead executor is respawned exactly once (US2 happy)
  Given a drive that exited 124 and a durable respawn budget of 1 with 0 used
  When gsd-run's post-drive handler runs
  Then the same drive argv is re-executed once and a GSD-RUN:RESPAWN attempt=1/1 line is emitted

Scenario: a productive non-timeout failure is not respawned (US2 error path)
  Given a drive that exited 1 and a run worktree with at least one commit past the phase base
  When gsd-run's post-drive handler runs
  Then no respawn occurs and the original nonzero rc is propagated unchanged

Scenario: session limit checkpoints instead of sleeping (US3 happy)
  Given a drive capture whose tail contains a vendor usage-limit banner with a parseable reset time
  When gsd-run's post-drive handler runs
  Then the run's lifecycle record becomes waiting with a wake_at timestamp and the process exits without sleeping

Scenario: unparseable banner fails safe and loud (US3 error)
  Given a drive capture tail matching the limit pattern but with no strictly-valid reset time
  When the banner evaluator parses it
  Then it exits nonzero with a typed SESSION-WAKE:unparseable line and no waiting record is written

Scenario: infra CI failure is rerun under a durable budget (US4 happy)
  Given a waiting ci-completed record whose pinned run finished with an infra-shaped failure and 0 of 2 reruns used
  When reconcile.sh evaluates it
  Then gh run rerun --failed is invoked once, the durable rerun counter becomes 1, and the record stays waiting

Scenario: real test failure is terminal (US4 error)
  Given a waiting ci-completed record whose pinned run finished with a test-assertion failure
  When reconcile.sh evaluates it
  Then the record transitions to failed with a typed CI-WATCH:test-failure line and no rerun is invoked

Scenario: oversized plan is refused at decompose (US5 happy)
  Given a generated plan file of 301 lines and FFS_PLAN_MAX_LINES unset
  When plan-length-gate.sh runs on its phase directory
  Then the command exits 1 with a typed PLAN-LENGTH: line naming that file and its line count

Scenario: compliant plans pass silently (US5 clean path)
  Given a phase directory whose every plan is at most 300 lines
  When plan-length-gate.sh runs on that phase directory
  Then the command exits 0 with no output

## Acceptance Criteria

- AC-001: `plan-wall.sh --await <secs>` polls at `PLAN_WALL_AWAIT_POLL` (default 15s) with a
  three-way exit contract: rc 0 = every plan PASS-class (`reviewed-pass|adjudicated-pass|
  pass-residual|WAIVED`); rc 20 = all terminal, ≥1 blocking (`blocked|WALL-ROUND-CAP`),
  typed `WALL-AWAIT:decided-blocked`; rc 75 = deadline with undecided plans. A record
  counts ONLY if its `.run_id` matches the current run AND its `.plan_sha256` matches the
  plan file's current hash — stale/foreign/forged records are pending. Plan set resolved
  ONCE at invocation; empty set → rc 1 typed. Strictly read-only: never creates/modifies
  records, never dispatches a reviewer, never touches the round counter (byte-identical
  round-counter file proven in tests).
- AC-002: `lifecycle.sh` writes one atomic (tmp+mv) JSON record per run with the schema in
  the architecture table; `validate` rejects unknown states/condition types; illegal
  transitions (e.g. `quarantined→runnable`, `done→waiting`) are typed rc-1 errors; every
  transition emits `LIFECYCLE:<from>><to> run=<id> reason=<r>`; budgets only ever
  decrement — a resumed invocation can never reset them.
- AC-003: `skills/feature-implement/SKILL.md` carries the `## Wall await rule` section
  (foreground default; background ⇒ await-loop in-turn; turn-end wall-pending ⇒ durable
  waiting checkpoint required); `skills/spec-decompose/SKILL.md` requires the plan-length
  gate; both lint-enforced by `tests/test_host_dispatch_lint.py`.
- AC-004: gsd-run post-drive: rc 124 → respawn regardless of commit count; other nonzero
  rc → respawn only when the commit probe (fail-CLOSED on git error) shows zero commits;
  `quarantined` status → NEVER respawn; budget `FFS_RESPAWN_MAX` (default 1, 0 disables)
  durable in the lifecycle record; respawn re-uses the identical argv + host (no
  cross-vendor replay); attempt 2 appends to the log (`>>` + wait on the tee process
  substitution) with the RESPAWN line written into the log, and runs under the ORIGINAL
  timeout minus elapsed.
- AC-005: session-limit handling: banner matched only in the capture TAIL (last 50 lines);
  extracted time must match strict `HH:MM` (+ am/pm/tz variants) BEFORE any arithmetic;
  past-time rolls to tomorrow; `wake_at = min(reset_epoch, now + FFS_SESSION_WAKE_MAX_SECS)`
  (default 21600); the run checkpoints `waiting(time)` and EXITS — no in-process sleep, no
  lock held; failing drive + non-empty capture + no banner → distinct `SESSION-WAKE:no-banner`
  WARN (banner drift greppable); wake relaunches counted durably
  (`FFS_SESSION_WAKE_MAX_ATTEMPTS`, default 4).
- AC-006: `reconcile.sh` is one idempotent bounded pass (no loop, no sleep beyond
  per-evaluator seconds-scale probes): `waiting`+satisfied → `runnable` → relaunch
  `resume_argv` (decrement budget durably, typed `RECONCILE:relaunched`); unsatisfied →
  untouched + `RECONCILE:still-waiting`; `quarantined|done|failed` → untouched; store
  access under the spec-009 coord claim so two concurrent reconcilers cannot double-launch
  (loser exits typed rc 0); `FFS_RECONCILE=off` → typed no-op rc 0; a relaunch that
  exhausts its budget transitions the record to `failed` with a typed budget line.
- AC-007: ci-completed evaluation: the watched run's `databaseId` is pinned at record
  creation (never re-resolved); classification runs only when the run's `attempt` has
  increased past the last classified attempt; infra-shaped failure (runner lost, system
  cancellation, startup_failure, network/dns/timeout/429/disk keyword table) →
  `gh run rerun --failed` under the durable `ci_reruns ≤ FFS_CI_RERUN_MAX=2` budget;
  any other failure → record `failed`, typed `CI-WATCH:test-failure`; nonzero `gh` exit =
  idle (bounded consecutive-failure cap), never a classification.
- AC-008: `plan-length-gate.sh` exits 1 listing EVERY over-limit plan
  (`PLAN-LENGTH:<file>:<lines>:<limit>`), rc 0 silent when compliant, rc 1 typed on an
  empty phase dir; limit `FFS_PLAN_MAX_LINES` default 300, total line count.
- AC-009: `skills/spec-decompose/SKILL.md` runs the gate after plan-phase output, before
  the coherence gate; over-limit routes to replan, never hand-truncation.
- AC-010: every new script passes `shellcheck -S warning`, has a usage block, a single
  `fail()` exit path, and typed `PREFIX:` lines per the repo error-taxonomy convention.
- AC-011: `docs/healing.md` documents the lifecycle (states, transitions, wake-condition
  types, budgets, kill switches, typed lines, exit codes), the operator cron/launchd line,
  a per-mechanism "delete when vendor ships X" sunset note, the n=1 evidence caveat, and
  the success metric spec-011's retro will measure (intervention-free completion,
  wall-clock/active ratio); README gains an index row.
- AC-012: no mechanism weakens an existing gate: with lifecycle active, tamper scan,
  evidence gates, wall verdict semantics, and budget quarantine behave identically on a
  stall-free run (existing suites stay green unmodified except where this spec adds tests).

## E2E Test Paths

- PATH-001: wall round trip — background wall on a fixture phase; --await returns 0
  mid-loop on pass records; rc 20 on a blocked record; stale-sha record stays pending
  (bats, stubbed reviewer).
- PATH-002: dead-executor round trip — fixture drive exits 124 with zero commits; gsd-run
  respawns once (stub succeeds second time); run status ends `completed`; log carries
  exactly one `GSD-RUN:RESPAWN` and attempt 1's lines (bats, stubbed drive).
- PATH-003: session-limit round trip — fixture capture with reset 3s out; gsd-run
  checkpoints `waiting(time)` and exits; a reconcile pass after 3s relaunches a stub
  resume that records its argv (bats).
- PATH-004: CI round trip — stubbed `gh`: in-progress → infra-failure → (attempt 2)
  success; two reconcile passes: first reruns (counter 1/2, still waiting), second
  relaunches on success (bats).
- PATH-005: oversized-plan refusal — 301-line plan → gate rc 1 naming it; 300-line plan
  passes (bats).
- PATH-006: reconciler safety — a quarantined record and a foreign-session-claimed run
  are both left untouched across a pass (bats).

## Edge Cases

- EDGE-001: verdict record exists but `.plan_sha256` mismatches (plan edited after review)
  → pending, never done; a replan invalidates its own wall pass.
- EDGE-002: verdict record from a previous run (`.run_id` foreign) → pending; yesterday's
  pass cannot satisfy today's wall (eng C1).
- EDGE-003: lifecycle record JSON corrupt/truncated → `validate` rc 1 typed; reconciler
  skips it with `RECONCILE:invalid-record`, never relaunches from garbage.
- EDGE-004: two reconcile passes race → spec-009 coord claim; loser exits typed rc 0,
  zero double-launches.
- EDGE-005: banner reset time exactly `now` → wake_at = now (no negative, no rollover);
  reconcile relaunches on its next pass.
- EDGE-006: banner in capture but drive rc 0 (limit warned, work finished) → no checkpoint,
  normal completion path.
- EDGE-007: `gh run rerun` succeeds but the rerun fails with the SAME infra shape →
  second rerun (2/2), then record failed `rerun-exhausted` — durable counter spans passes.
- EDGE-008: respawn budget exhausted AND still zero commits → original rc propagates,
  lifecycle record `failed`, typed line; never a third attempt.
- EDGE-009: `FFS_LIFECYCLE=off` mid-run (set between checkpoint and reconcile) →
  reconciler still honors existing records (off gates new checkpoints, not recovery of
  already-written ones); documented.
- EDGE-010: plan file added to the phase AFTER --await resolved its plan set → not
  awaited this invocation (set pinned at start); the wall itself still gates it at the
  pre-execution seam — no execution bypass.
- EDGE-011: empty phase dir → --await rc 1 typed AND plan-length-gate rc 1 typed (aligned
  semantics, no synthesized filenames, no 30-minute wait).
- EDGE-012: `wake_at` in the record but vendor still limited at relaunch → the relaunched
  drive fails with the banner again → new checkpoint under the durable wake budget
  (default 4) → after budget, record `failed` typed.

## E2E stubs (bats — this repo's E2E layer; no browser surface)

One bats round-trip test per PATH-001..006 as named in `## E2E Test Paths`, living in the
test files mapped in plan.md's TDD table (stubbed reviewer / stubbed drive / stubbed gh /
fixture captures; hermetic per the deps.bats precedent).

## Test Contract Summary

| Layer | Count | Status |
|---|---|---|
| BDD Scenarios | 14 | draft |
| Unit test cases | 40 | listed |
| Unit test files | 8 | mapped |
| Integration tests | 5 | defined |
| E2E paths (bats round trips) | 6 | defined |

## Non-goals

- No long-lived daemon shipped; no crontab installed by FFS (operator installs the
  documented line if they want continuous reconciliation).
- No retry of gate verdicts (blocked walls stay blocked; quarantine is terminal for
  auto-healing).
- No vendor billing/quota API parsing — only the CLI's own banner text.
- No changes to wall review policy, round caps, or diminishing-returns logic (PR #91/#96).
- Spec 006 (autonomous landing) untouched.
