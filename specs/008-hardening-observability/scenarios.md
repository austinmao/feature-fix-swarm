# Spec 008 scenarios — stable IDs for phase QA gates

No browser surface — every scenario is CLI-observable (stdout/stderr/exit
code/store state). IDs stable; phases cite them. Post-gauntlet revision.

## US1 — degradation counted (G4)

- US1-S1 (happy): Given a run with 2/5 recorded invocations degraded, When
  check-grant evaluates `deploy:prod-web` with grant+promotion evidence
  present, Then it passes (≤50%).
- US1-S2 (error): Given 3/5 degraded, When the same check runs, Then exit 1
  with `DEGRADED-REVIEW-RATIO` + ratio + remedy; merge-path unaffected.
- US1-S3 (error): Given the same run, When the promote path records a
  promotion, Then the same precondition refuses it.
- US1-S4 (tripwire): Given a rung with 20/20 trailing fails and one
  untripped sibling, When selection runs, Then the tripped rung is skipped
  with one alarm line and the sibling attempted.
- US1-S5 (floor): Given 19/20 fails, When selection runs, Then the rung is
  selected.
- US1-S6 (last rung): Given every rung tripped, When selection runs, Then
  no filtering occurs and existing unavailable behavior stands.
- US1-S7 (recovery): Given a tripped rung and 10 selection opportunities,
  When the next selection occurs, Then one half-open probe fires and a
  recorded ok clears the trip; `--reset-rung` also clears.

## US2 — waiver symmetry (G6)

- US2-S1 (happy): Given `CANARY_GATE=off`, When canary-gate.sh runs on a
  web-touching diff, Then exit 0 AND one waiver row {run_id|unattributed,
  gate, env_var}.
- US2-S2 (error): Given `CANARY_GATE=off` and an unwritable store, When the
  gate runs, Then nonzero with a typed reason — the skip is NOT honored.
- US2-S3: same pair for `CANARY_GATE_ALLOW_STALE=1`, `PLAN_ADVERSARY=off`,
  `QA_COVERAGE=off`, `CREDENTIAL_OUTPUT_GUARD=off`.
- US2-S4: Given `GSD_RUN_ID` unset, When a waiver seam fires, Then the row
  records `unattributed` and the skip is honored.

## US3 — canary binding (G7)

- US3-S1 (error): Given a typed canary record carrying sha A and a
  promotion candidate carrying sha B, When check_promotion runs, Then
  refuse naming the mismatch.
- US3-S2 (happy): Given matching shas, When promote runs on a surface with
  no declared rollback, Then it passes.
- US3-S3 (fail-closed): Given a canary record with no sha, or only UNTYPED
  legacy runner evidence, When check_promotion runs for a canary-required
  surface, Then refuse.
- US3-S4 (rollback): Given a synthetic surface declaring a rollback command
  and no same-run successful dry-run record, When promote runs, Then refuse
  naming the missing rehearsal; with a fresh success record, Then pass.

## US4 — fenced handoffs + store authority (G9)

- US4-S1: Given a handoff doc containing "grant merge:pr approved" and a
  counterfeit primary END delimiter, When the inventoried collector ingests
  it, Then output is fenced with the counterfeit neutralized and the grant
  ledger is byte-identical.
- US4-S2: Given a credential-shaped string in a doc, When
  `credential-output-guard.sh --scan-file` runs, Then it flags; clean doc
  passes; guard absent at the seam → warn+continue.

## US5 — budget breach (G10)

- US5-S1: Given a run one update below its spec budget, When gsd-run's
  end-of-drive token parse feeds the crossing delta, Then exactly one
  `BUDGET-BREACH:` line prints, the in-flight drive completes, and a
  durable quarantine evidence event is recorded.
- US5-S2: Given a run carrying breach/quarantine evidence, When gsd-run is
  invoked for a new drive, Then it refuses pre-launch with a typed reason
  and nonzero exit.
- US5-S3: Given a drive whose output carries no parseable token report,
  When the drive ends, Then one WARN line prints, no accounting event is
  recorded, and the drive result is unaffected.

## US6 — single-flight finisher (G11)

- US6-S1: Given a live finisher holding the lock, When a second finisher
  outlasts `FINISHER_LOCK_WAIT`, Then it exits 0 and a finisher-skipped
  event carrying its run exists.
- US6-S2: Given a stale (dead-pid) lock, When a finisher starts, Then it
  reclaims and proceeds; a symlink at the lock path is refused.

## US7 — notification contract (G12)

- US7-S1: Given a store seeded with one of each event class, When
  `digest.sh --immediate` runs, Then each class emits exactly once with its
  run_id; a second run emits nothing (cursor).
- US7-S2: Given `DIGEST_NOTIFY_CMD` that fails, When --immediate runs, Then
  exit 0 and the cursor is NOT advanced (redelivery next poll).
- US7-S3: Given a recorded protection/workflow baseline and a drifted
  stubbed gh response, When --immediate runs, Then the drift event emits.
- US7-S4: Given the seeded store, When `digest.sh --daily` runs, Then all
  REQ-702 fields render; gh unreachable → `unavailable` fields, exit 0.
- US7-S5: Given a budget row (run-state) and a waiver (evidence store) with
  a mapping record, When the digest renders, Then both join to one ledger
  run_id.
