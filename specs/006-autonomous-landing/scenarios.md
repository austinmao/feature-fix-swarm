# Scenarios — spec 006 (BDD, stable IDs)

No browser surface — all scenarios execute as bats over real-git fixtures with
stubbed vendors (gh, CLIs, implementer). IDs are stable for phase QA gates.

## US1 — takeover record + wall

US1-S1 (record written)
  Given a spec run with a resolvable evidence store and grant ledger
  When  /spec-status runs for that spec
  Then  <store>/takeover/<run-id>.json exists with ids, git state, grants,
        pendings, forbid[] and resume fields, plus a .md prose superset

US1-S2 (decoy store refused)
  Given a takeover record whose gates_store realpath differs from the resolved store
  When  takeover-check.sh runs for that run-id
  Then  it exits nonzero printing TAKEOVER-REFUSED:decoy-store plus its
        one-command remedy, before any ledger read

US1-S3 (clean handoff passes)
  Given a takeover record whose 9 invariants all hold live
  When  takeover-check.sh runs
  Then  it prints TAKEOVER-OK and exits 0 in under 5 seconds

US1-S4 (no record = no-op)
  Given a run with no takeover record in the store
  When  feature-implement Step 2 reaches the wall line
  Then  the wall is a silent no-op and the run proceeds exactly as today

## US2 — /land-queue

US2-S1 (item lands)
  Given a takeover record for a finished spec branch with fresh preflight and grants
  When  /land-queue runs
  Then  the item is rebased, finished, CI-waited, merged under its merge:pr
        grant, journaled with its merge SHA, finalized, and reported LANDED

US2-S2 (red item does not stop the queue)
  Given a queue of three items where the second fails its gate twice
  When  /land-queue runs
  Then  item two reports BLOCKED:no-progress with a one-command unblock in the
        Human inbox, and items one and three still reach terminal states

US2-S3 (systemic abort)
  Given two consecutive items blocking with the same enumerated systemic class
  When  the second block is recorded
  Then  the queue stops with QUEUE-ABORTED:systemic:<class> and unprocessed
        items report SKIPPED:queue-aborted

US2-S4 (CI timeout never merges)
  Given an item whose PR checks are still pending at the bound
  When  the bounded CI wait expires (rc 124)
  Then  the item reports BLOCKED:ci-timeout and no merge is attempted

US2-S5 (crash-safe resume)
  Given a queue crashed after an item's merge succeeded but before its LANDED append
  When  /land-queue --resume runs
  Then  the item reconciles to LANDED from assert-merged evidence with no
        second merge attempt

US2-S6 (operator stop)
  Given a running queue and an operator who creates <store>/land-queue/STOP
  When  the current item reaches its next checkpoint
  Then  the queue stops with QUEUE-ABORTED:operator-stop and nothing new starts

## US3 — consolidate 4-A + posture

US3-S1 (conjunction refusal)
  Given a consolidate run missing any one of grant, fresh estate, set proof, or
        assert-merged evidence
  When  Step 4-A --autonomous runs
  Then  it refuses with exit 1 naming the missing evidence item

US3-S2 (report-only default)
  Given a consolidate run with the full 4-part conjunction
  When  Step 4-A runs without --execute
  Then  it prints the per-target deletion plan with evidence tuples and
        deletes nothing

US3-S3 (zero posture degrades privilege)
  Given autonomy.posture zero and a run whose reviews exceeded the degraded bound
  When  the run attempts promotion to prod
  Then  promotion is refused while merge remains permitted, with the
        degradation count in the report

US3-S4 (floor posture blocks)
  Given autonomy.posture floor and no cross-vendor reviewer reachable
  When  a queue item reaches its review step
  Then  the item reports BLOCKED:no-cross-vendor-reviewer and no same-host
        review substitutes
