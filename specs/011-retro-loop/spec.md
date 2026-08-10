# spec-011 — Self-healing retro loop (/retro-analyze + /retro-triage)

## Context

FFS heals itself in two layers. Spec-010 (merged: PR #109) closed the *liveness*
layer — durable lifecycle records, a reconciler, respawn, wake evaluators. This
spec closes the *learning* layer: every run ends with a scrubbed, deduped,
graded feedback pass that files issues to `austinmao/feature-fix-swarm`, and a
maintainer-side triage skill batches those issues into `/feature-spec` briefs.
Consent is collected once at `/ffs-init` onboarding (Part A shipped ffs-init
Phase 0; the interview is the natural home).

Evidence this loop would already have paid for itself — defect classes observed
during the spec-010 run itself, none of which were filed anywhere durable:
wall re-litigation that never converges to 0-HIGH; availability-failure review
rounds poisoning diminishing-returns baselines; dual run-state stores needing
hand-syncs; `gates-test-command.sh` recording evidence under an empty key when
invoked bare; ~40 environmental installer-test failures polluting every runner
worktree baseline; drive tool-sessions externally terminating long suite runs.
Today each of these lives only in operator memory and session handoffs.

The two success metrics named in `docs/healing.md` are this spec's to measure:
**intervention-free completion rate** and the **wall-clock/active ratio** —
spec-010 built the mechanisms; this retro reports whether they work.

Non-goals: no server-side collector (GitHub issues ARE the collector); no
LLM-authored issue prose from consumer runs (allowlist facts only); no
telemetry beyond the retro payload; no auto-fix — filing and triage only.

## User Stories

- US1 (consumer): as an FFS user who consented, when any FFS run finishes, run
  diagnostics are analyzed and real defects are filed upstream automatically,
  without leaking anything about my code, paths, or repo.
- US2 (consumer, privacy): as a user who did NOT consent (or set kill
  switches), nothing is collected and no network call is made.
- US3 (maintainer): as the FFS maintainer, `/retro-triage` turns the incoming
  `source/ffs-retro` issue stream into prioritized `/feature-spec` batch
  briefs, dedup-collapsed and ordered by priority.
- US4 (maintainer, labeling): as the maintainer, incoming retro issues are
  auto-labeled (`priority/Px`, `source/ffs-retro`, `triage`) even though
  non-collaborator consumers cannot set labels.

## BDD Scenarios

Feature: post-run retro analysis with consent-gated upstream filing

Scenario: consented run files a graded issue
  Given a consumer repo whose user recorded retro consent and a completed run whose digest contains a P1-classifiable event
  When the run finalizer's retro tail executes
  Then a GitHub issue exists on austinmao/feature-fix-swarm whose body contains only allowlisted FFS-internal facts and a machine-readable fingerprint comment

Scenario: no consent means no collection and no network
  Given a consumer repo with no recorded retro consent
  When the run finalizer's retro tail executes
  Then the tail exits 0 with a typed no-op line and zero gh invocations occurred

Scenario: scrub rejection blocks filing entirely
  Given a consented run whose candidate payload contains a non-allowlisted value (an absolute path)
  When the retro scrub validates the payload
  Then nothing is filed, the pass exits nonzero with a typed line, and zero gh invocations occurred

Scenario: duplicate finding becomes a comment, not a new issue
  Given an open retro issue whose body carries fingerprint F and a new run producing the same fingerprint F
  When the retro filing step runs
  Then the existing issue gains one occurrence-bump comment and no new issue is created

Scenario: retro failures never break the consumer run
  Given a consented run and a retro.sh that fails internally (gh unreachable)
  When the run finalizer's retro tail executes
  Then the finalizer's exit status is unchanged and the failure is recorded only in the local ledger

Scenario: maintainer triage produces a prioritized brief
  Given open issues labeled source/ffs-retro with priority labels P1 and P2
  When the maintainer invokes /retro-triage in the FFS repo
  Then a batch brief is emitted ordering P1 before P2 with one /feature-spec-ready section per cluster

Scenario: triage refuses to run outside the FFS repo
  Given a checkout whose origin remote is not austinmao/feature-fix-swarm
  When the maintainer invokes /retro-triage
  Then it exits nonzero with a typed refusal and reads nothing

Scenario: consent interview stores a user-scope decision
  Given a fresh machine with no consent record
  When /ffs-init runs its onboarding interview and the user accepts the retro question
  Then ~/.cache/feature-fix-swarm/consent.json records the grant and no consumer-repo file is written

## Acceptance Criteria

- AC-001: `retro.sh check-consent` is fail-closed: missing, corrupt, or
  ungranted consent file → rc 0 typed no-op; `FFS_RETRO=off` env and
  `--no-retro` flag short-circuit BEFORE any collection; `gh auth status`
  failure → silent no-op plus local ledger row. Zero `gh` write calls in all
  four cases (stub-asserted).
- AC-002: `retro.sh collect` assembles the payload ONLY from FFS-owned facts:
  digest event classes (`.feature-fix-swarm/digest-*.jsonl`), findings-queue
  sig/severity, exit codes, phase durations, model tiers, FFS version, FFS
  script names. Free text from the consumer run, absolute paths, repo names,
  and code content never enter the payload. `suggested_fix` may reference FFS
  files only.
- AC-003: grading is deterministic from the payload: P0
  security/scrub/grant-bypass or data loss · P1 dead executor / unrecovered
  stall / wall-clock ≥2× active on one event class · P2 degraded-but-shipped
  (fallback, retry, gate WARN) · P3 optimization note.
- AC-004: the fingerprint is
  `sha256(script|event_class|gate|exit_code|ffs_minor)[:16]` — stable across
  runs, machines, and repos; timestamps, durations, and run ids are excluded.
- AC-005: scrub is defense-in-depth and fail-CLOSED: `lib/retro_scrub.py`
  schema-validates against an enum/numeric-first allowlist (keys with
  per-key value regexes, 500-char cap) and REJECTS any path-shaped or
  URL-shaped value — `/Users/`, `/home/`, `/private/`, `/tmp/`, Windows
  drive paths, `file://`, percent-encoded paths, credential-bearing URLs,
  and the consumer repo's owner/name — except explicitly-allowlisted
  FFS-relative filenames; the payload is copied to a 0600 temp file and
  `scan-handoff-credentials.sh` runs on that exact copy BEFORE any `gh`
  call; a MISSING or erroring scanner is itself a reject. ANY reject → rc 1,
  nothing filed, zero `gh` calls.
- AC-006: dedup-then-file with GitHub's realities: issue lookup requests
  explicit `--json number,title,body` with a bounded page (≥200) plus a
  fingerprint `--search` fallback; exact fingerprint match → `gh issue
  comment` occurrence bump; title similarity ≥ `RETRO_TITLE_SIM` (default
  0.8, difflib) → comment on the closest issue; else RE-QUERY the
  fingerprint immediately before `gh issue create` (eventual-consistency +
  cross-machine race narrowing — a surviving cross-machine duplicate is
  accepted and merged maintainer/workflow-side). Metadata rides as an HTML
  comment (`<!-- ffs-retro fingerprint:… priority:… occurrences:… -->`,
  format version v1).
- AC-007: noise caps hold under concurrency: one user-scope lock (flock on
  `~/.cache/feature-fix-swarm/retro.lock`) serializes the whole file verb —
  create cap (`RETRO_MAX_NEW_ISSUES`, default 3), P3 ledger accrual
  (`RETRO_P3_OCCURRENCE_FLOOR`, default 3), and the persisted last-write
  timestamp enforcing ≥2s pacing between `gh` writes — so two same-machine
  runs cannot over-create, corrupt the ledger, or double-fire the P3 floor.
- AC-007b: gh write authorization is fail-soft: 403/404/422 (no account
  scope, issues disabled, restricted repo) → typed line + local ledger row,
  rc 0, no retry queue.
- AC-008: the finalizer seam is one fail-soft tail block after the existing
  digest call (`run-finalizer.sh` G12 seam, same `run()` wrapper) covering
  `feature-implement`, `fix`, and `task-swarm`; `feature-spec` gets one
  identical tail line. Every path through the tail exits 0 — a retro failure
  can never change a consumer run's exit status.
- AC-009: consent lives user-scope at
  `~/.cache/feature-fix-swarm/consent.json` (never in the consumer repo),
  asked once in the `/ffs-init` interview with recommended-yes framing and
  plain-language allowlist description; headless/no-answer = no consent;
  `retro.sh consent --revoke` revokes; re-asked only on `--reset` or major
  version bump.
- AC-010: `/retro-triage` refuses (typed line, rc 1) unless the repo's origin
  remote is `austinmao/feature-fix-swarm`; when it runs, it reads
  `source/ffs-retro` issues, clusters by fingerprint/similarity, orders by
  `priority/P0..P3`, and emits one batch brief per cluster in
  `/feature-spec`-consumable form.
- AC-011: `.github/workflows/retro-label.yml` triggers on `issues: opened`
  AND `issue_comment: created`, guards against its own actor (no recursion),
  applies `priority/Px`, `source/ffs-retro`, and `triage` labels (creating
  labels idempotently), and OWNS the occurrence count: it parses the
  consumer's bump comment and edits the metadata HTML comment in the issue
  body — consumers only create/comment (non-collaborators cannot label or
  edit).
- AC-012: the retro never reports on itself: the finalizer runs it last, it
  excludes `script=retro.sh` events from its own payload, and its own
  failures go only to the local ledger.
- AC-013: the retro payload includes the two healing metrics with
  deterministic derivation: wall-clock = last minus first digest event
  timestamp; active = sum of recorded phase durations; intervention-free =
  no operator-intervention event class present in the digest. A metric whose
  inputs are absent is OMITTED (never guessed), and both derivable and
  non-derivable digest fixtures are tested.
- AC-014: user-scope state hygiene: `~/.cache/feature-fix-swarm/` is created
  0700; consent.json and retro-ledger.jsonl are written atomically at 0600;
  a symlinked or non-regular consent/ledger file is treated as ABSENT
  (no-consent, empty ledger) with a typed warning — tampered state can
  disable the retro but never redirect its writes.
- AC-015: `/retro-triage` briefs are built from validated metadata and
  allowlisted factual fields only; arbitrary issue titles/bodies/comments
  are escaped or omitted, never passed through as instruction-bearing prose
  (issue-body prompt injection cannot reach a `/feature-spec` agent through
  a brief).
- AC-016: `ffs_minor` is the `major.minor` of the topmost CHANGELOG release
  heading, normalized (`X.Y`); unknown/dev states normalize to `0.0` —
  boundary-tested so identical defects never split across fingerprints by
  version-format drift.

## E2E Test Paths

- PATH-001: consented consumer run with a stubbed `gh` → finalizer tail →
  collect → grade → scrub → one issue created with allowlisted body +
  fingerprint comment.
- PATH-002: same facts filed twice → one create then one comment (occurrence
  bump), asserted against the gh stub's call log.
- PATH-003: no-consent run → typed no-op, gh stub records zero calls.
- PATH-004: scrub-reject fixture (`/Users/x` in a value) → rc 1, zero gh
  calls, nothing in the ledger marked filed.
- PATH-005: maintainer `/retro-triage` in the FFS repo over stubbed issue
  JSON → prioritized batch brief; outside the FFS repo → typed refusal.

## Out of scope

- Editing `lib/ffs_installer.py` / `doctor()` (installer session's surface).
- Any consumer-side LLM authoring of issue text.
- Auto-remediation from filed issues (triage emits briefs; humans launch
  specs).
- A hosted/telemetry backend of any kind.

## Edge Cases

- EDGE-001: digest file absent or empty on a consented run → typed
  `RETRO:no-events`, rc 0, zero gh calls (nothing to grade is not an error).
- EDGE-002: digest jsonl carries a torn/corrupt final line → that line is
  skipped; remaining events grade normally.
- EDGE-003: consent.json is a symlink or FIFO → treated absent (no-consent),
  typed warning (AC-014).
- EDGE-004: two same-machine runs finalize simultaneously → flock serializes;
  second run sees the first's ledger/cap state (AC-007, INT-006).
- EDGE-005: gh authenticated but token lacks repo scope (403 on create) →
  typed line + ledger row, rc 0, no retry (AC-007b).
- EDGE-006: issues disabled upstream (410/404) → same fail-soft path.
- EDGE-007: the fingerprint match lives on a CLOSED issue → comment on the
  closed issue anyway (maintainer sees recurrence) — never reopen, never
  create a duplicate.
- EDGE-008: payload value exactly 500 chars → passes; 501 → reject
  (boundary).
- EDGE-009: RETRO_MAX_NEW_ISSUES=0 → zero creates, comments still allowed
  (cap floors at comments-only, not full off — FFS_RETRO=off is the off).
- EDGE-010: CHANGELOG missing or headed by "Unreleased" only → ffs_minor
  `0.0` (AC-016).
- EDGE-011: clock skew makes wall-clock < active → ratio omitted (AC-013's
  omit-never-guess rule).
- EDGE-012: `retro.sh consent --revoke` while a filing pass holds the flock →
  revoke waits on the lock; the in-flight pass completes, the NEXT pass sees
  revoked.

## E2E Test Stubs (CLI repo — bats round trips, one per PATH-NNN)

```bash
# tests/bats/retro-e2e.bats — stubs; fixtures + gh stub call-log assertions
@test "PATH-001: consented run files one allowlisted issue with fingerprint" {
  # arrange: isolated HOME + consent granted + digest fixture (1 P1 event) + gh stub
  # act:     run bash scripts/gsd/retro.sh analyze
  # assert:  gh stub log shows 1 create; body ⊆ allowlist; metadata comment present
}
@test "PATH-002: same facts twice -> one create then one comment" {
  # arrange: run PATH-001 then re-run with identical fixture; stub lists the created issue
  # act:     second analyze
  # assert:  1 comment, 0 new creates
}
@test "PATH-003: no consent -> typed no-op and zero gh calls" {
  # arrange: isolated HOME, no consent.json, gh stub
  # act:     analyze
  # assert:  RETRO:no-consent line; stub call log empty
}
@test "PATH-004: scrub reject -> rc 1 and zero gh calls" {
  # arrange: consented; payload fixture carrying /Users/x
  # act:     analyze
  # assert:  rc 1 inside retro.sh; stub log empty; ledger records reject
}
@test "PATH-005: retro-triage brief in-repo; typed refusal out-of-repo" {
  # arrange: stubbed gh issue list JSON (2 clusters, P1+P2, injection title)
  # act:     triage in FFS repo, then in a scratch repo
  # assert:  brief orders P1 first, injection text absent; scratch -> rc 1 typed refusal
}
```

## Test Contract Summary

| Layer             | Count | Status  |
|-------------------|-------|---------|
| BDD Scenarios     | 8     | draft   |
| Unit test cases   | 24    | listed  |
| Unit test files   | 5     | mapped  |
| Integration tests | 12    | defined |
| E2E paths         | 5     | stubbed |
