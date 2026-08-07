# Spec 008 plan — hardening + observability sweep

Prior-art decision (from `prior-art.md`): **build-fresh for all seven
sub-systems** — every vindicated OSS candidate (pybreaker 689★, flagger
5385★, rebuff 1517★, filelock 974★, apprise 17010★, …) drags a dependency,
daemon, or K8s; rejected against the zero-dependency bash+python-stdlib
constraint. Patterns ported without code: flagger/argo
rollback-on-metric-breach → G7; tldrsec delimiter taxonomy → G9. Decisive
prior art is IN-REPO (seam table below).

## Decision Audit Trail (spec gauntlet, 2026-08-07)

Reviewers: codex (read-only sandbox, 15 findings incl. 4 CRITICAL), opus
fresh-context (12 findings incl. 1 CRITICAL). Dispositions are LOCKED —
planners implement, never reopen.

| # | src | finding | disposition |
|---|-----|---------|-------------|
| 1 | codex C1 + opus F1 | CRITICAL: no producer for G4 events; DEGRADED is one boolean; rung loop reports prose only; caller-side filtering misses ladders/exact requests | ADOPTED — central recording+filtering inside adversary-host (ladder AND exact path); REQ-101/103 |
| 2 | codex C2 | rung-attempt vs invocation conflated; ratios undefined | ADOPTED — two event schemas, canonical rung_id vendor:model:effort; REQ-101 |
| 3 | codex C3 | tripped rung can never auto-clear (never selected) | ADOPTED — half-open probe every 10th opportunity + operator reset; REQ-103 |
| 4 | opus F2 | EDGE-013's "mandatory HIGH path" is prose, not shell; filtering last rung = hard ship block | ADOPTED — never filter last remaining rung; EDGE-013 restated |
| 5 | codex C4 + opus F3 | CRITICAL: direct promote unguarded; hotfix bypasses ratio rule | ADOPTED (promote) — shared precondition at check_grant_prod AND record_promotion. Hotfix: DOCUMENTED EXEMPTION (audited bypass channel, posture domain) — dissent recorded: codex wanted it wired; red-team posture decision says hotfix self-service stays |
| 6 | opus F11 | ratio denominator undefined across seams | ADOPTED — all recorded invocations for the run, mixed-seam fixture pins it; AC-001 |
| 7 | opus F4 + codex C5 | delegation-enforcer is fail-open advisory; fail-closed waiver inverts contract; run_id resolution undefined | ADOPTED — dropped from waiver set; GSD_RUN_ID-or-`unattributed` rule; per-seam behavior pinned; REQ-201 |
| 8 | codex C13 | credential-output-guard has no file-scan mode; its kill-switch missing from symmetry | ADOPTED — `--scan-file` mode is explicit new work; switch joins waiver set; REQ-202 |
| 9 | opus F5 + codex C6 | CRITICAL: no artifact digest exists; third-party results can't be identity source; untyped evidence satisfies promotion | ADOPTED — HEAD commit sha recorded by canary-gate (trusted wrapper) into TYPED canary evidence; check_promotion requires the type for canary surfaces; REQ-301 |
| 10 | codex C7 | 10-min real-traffic window unfalsifiable (no traffic exists) | ADOPTED — descoped; timestamps recorded for future rule; REQ-301 |
| 11 | opus F6 + codex C9 | fallback-rehearsal.sh is model-CLI smoke, not a rollback lever; nothing to dry-run on FFS | ADOPTED — REQ-302 reshaped to schema+gate mechanism, synthetic-surface tests, no-op on FFS |
| 12 | codex C11 | wrong ingestion seams; docs legitimately carry resume commands | ADOPTED — reader inventory at plan time; store-authority rule narrowed to authority-bearing values; REQ-401 |
| 13 | codex C14 + opus F12 | fence doesn't neutralize PRIMARY markers; structural tests can't prove injection resistance | ADOPTED — shared fence fn neutralizing primaries in every branch; ACs restated as presence+store-authority; REQ-401/402 |
| 14 | opus F7 + codex C12 | CRITICAL: inc_tokens has 1 caller that discards return; no daily store; no scheduler; >= re-fires | ADOPTED — crossing detection (fires once), BUDGET-BREACH line, gsd-run consumer; DAILY DESCOPED (+EDGE-009); REQ-501 |
| 15 | opus F9 + codex (lock) | claim_pidfile lacks reclaim/symlink/params; those live in acquire_run_state | ADOPTED — extract full primitive parameterized; gsd-run migrates; REQ-601 |
| 16 | opus F10 | global lock silently drops run-B cleanup | ADOPTED — wait FINISHER_LOCK_WAIT then yield WITH finisher-skipped event; REQ-601 |
| 17 | opus F8 | org audit-log API 404 on personal repo (verified live) | ADOPTED — poll branch-protection + workflows tree sha vs recorded baseline; REQ-701 |
| 18 | codex (G12 mechanics) | immediate mode had no trigger/cursor/transport/failure signal | ADOPTED — durable cursor, finalizer+cron invocation, DIGEST_NOTIFY_CMD, cursor-retained-on-failure; REQ-701 |
| 19 | codex C15 | run_id join broken (spec-NNN vs random runstore hex vs gh) | ADOPTED — ledger id canonical + mapping record at run start; budgets join via mapping; REQ-703 |
| 20 | opus F1 anchor | review-gate DEGRADED anchor is :98-99/:117, not :29 | ADOPTED — seam table corrected |

## Verified seam anchors (scout + gauntlet corrections, main @ a8866f7)

| Seam | Anchor | Note |
|---|---|---|
| degraded flag | `review-gate-command.sh:98-99,117` | one boolean per invocation — the INVOCATION event source |
| rung ladder | `adversary-host.sh:78-118` (ladder loop), `:302-437` (selection/exact) | central recording + tripwire filter live HERE |
| waiver pattern | `plan-wall.sh:482` (`_pw_waiver_path`) | extracted into waiver-record.sh |
| kill-switch seams | `canary-gate.sh:60`, `:116` (ALLOW_STALE); `plan-adversary.sh:23/:49`; `qa-coverage-adversary.sh:5/:40` | delegation-enforcer dropped (advisory hook) |
| fence idiom | `plan-wall.sh:273-329` | neutralizes SOCRATIC markers only — primary-marker gap is REQ-401 work |
| budget | `state.py:196` (`inc_tokens`→None; event `:212`); sole caller `run_state/cli.py:81` | crossing detection + machine-readable line |
| lock primitive | `gsd-run.sh:308` (`claim_pidfile` — minimal), `:336+` (`acquire_run_state` — reclaim/symlink/lease) | extract the LATTER, parameterized |
| promotion comparison | `gates.py:875` (`check_promotion`), `:767` (`_evidence_resolves`), `:756` (`_valid_artifact`) | typed canary evidence + sha match |
| hotfix routing | `gates.py:1083-1090` (`_check_hotfix_bypass` before prod dispatch) | documented ratio exemption |
| CLI dispatch | `gates.py:1627+` if/elif | note-degraded + waiver subcommands slot in |
| canary HEAD sha/time | `canary-gate.sh:26` (freshness header), `:108-116` | wrapper records HEAD sha into typed evidence |
| finalizer patterns | `run-finalizer.sh:60-61` | digest emitters + finisher lock entry |
| credential guard | `scripts/hooks/credential-output-guard.sh` | PreToolUse JSON parser today; gains --scan-file |

Anchors drift — phase research re-verifies before editing.

## Architecture decisions (post-gauntlet)

1. **G4 storage**: evidence store namespaces `_review_events` (invocations,
   run-scoped) + `_rung_attempts` (global FIFO per rung, cap 20) via
   `note-degraded` subcommand; `--tripped` lists tripped rungs;
   `--probe-due <rung>` answers the half-open question (selection counter
   in the same namespace). Bash consults python by CLI only.
2. **G4 enforcement**: one `_degraded_ratio_blocks(run_id)` helper called
   from `check_grant_prod` AND the promote/record_promotion path; hotfix
   exempt (documented).
3. **G6 recorder**: `scripts/gsd/waiver-record.sh <gate> <env_var>` →
   `gates.py waiver` subcommand; run_id from GSD_RUN_ID else
   `unattributed`; nonzero on store failure and the calling seam does NOT
   skip. plan-wall migrates.
4. **G7**: typed canary evidence kind `canary` written by canary-gate.sh
   {sha, timestamps, pass contract}; `check_promotion` requires kind+sha
   match for canary surfaces; `rollback_dryrun` kind + declared-command
   gate, no-op on FFS, synthetic-surface tests.
5. **G9**: `scripts/gsd/fence-data.sh` (primary-marker neutralization all
   branches); reader inventory task precedes wiring; store-authority
   fixture test; credential-guard `--scan-file` + waiver-set membership.
6. **G10**: crossing detection in `inc_tokens` (returns typed breach once);
   `BUDGET-BREACH:` line from cmd_update; gsd-run drive-loop consumer
   quarantines remaining tasks. No daily anything.
7. **G11**: extract acquire/reclaim/release from acquire_run_state into
   `scripts/gsd/lib-lock.sh` (parameterized path/lease); gsd-run sources
   it (behavior-compatible, bats-pinned); finisher wait-then-yield-with-
   mark at `~/.cache/feature-fix-swarm/finisher.lock` (host-scoped,
   documented).
8. **G12**: digest.sh two modes; cursor file beside store; classes per
   REQ-701 incl. protection/workflow drift vs recorded baseline (gh
   stubbed in tests); DIGEST_NOTIFY_CMD transport; mapping record
   {ledger_run_id, runstore_id} written by gsd-run at start (REQ-703).

## Unit Test List

- [ ] note_degraded: invocation event run-scoped (no run_id → usage error); rung attempt global; FIFO cap 20
- [ ] degraded_ratio: >50% refuses check_grant_prod AND record_promotion; ==50% passes; merge unaffected; mixed-seam denominator fixture; hotfix unchanged
- [ ] tripwire: 20/20 trips; 19/20 no; <20 no; --tripped lists; probe-due at 10th opportunity; ok clears; --reset-rung clears
- [ ] waiver subcommand: row shape; unattributed fallback; store-failure nonzero; _StoreLock serialized
- [ ] check_promotion: typed canary sha match/mismatch/missing; untyped legacy evidence refused for canary surface; freshness independent
- [ ] rollback gate: declared-surface matrix (absent/other-surface/other-run/failed/success); FFS no-op
- [ ] inc_tokens: None below; breach once at crossing; idempotent past limit; cmd_update line once
- [ ] mapping record + digest join: budget row + waiver row → one ledger id
- [ ] reasons: DEGRADED-REVIEW-RATIO survives sanitize_reason with remedy

## TDD Unit Test Map

| Source | Test file | Behaviors |
|---|---|---|
| lib/gates.py | lib/tests/test_gates.py (`# ── spec-008` section) | note-degraded, ratio rule both seams, tripwire, waiver subcommand, canary binding, rollback gate |
| lib/run_state/state.py + cli.py | lib/tests/test_run_state*.py | crossing detection, breach line, mapping record |
| scripts/gsd/waiver-record.sh | tests/bats/waiver-record.bats | 5 switches × write-then-skip + fail-closed |
| scripts/gsd/adversary-host.sh | tests/bats/adversary-host.bats (extend) | central recording both paths; filter; last-rung; probe |
| scripts/gsd/canary-gate.sh | tests/bats/canary-gate.bats (extend) | typed evidence + sha; ALLOW_STALE waiver |
| scripts/gsd/fence-data.sh + consumers | tests/bats/fence-data.bats | primary neutralization; hostile-doc fixture; ledger byte-identical |
| scripts/hooks/credential-output-guard.sh | tests/bats/credential-guard.bats | --scan-file hit/clean; off-switch waiver |
| scripts/gsd/lib-lock.sh + run-finalizer.sh | tests/bats/lib-lock.bats + run-finalizer.bats (extend) | concurrency, reclaim, symlink, wait-then-yield-with-mark; gsd-run compat |
| scripts/gsd/digest.sh | tests/bats/digest.bats | classes, cursor idempotence, notify-failure redelivery, drift-vs-baseline, daily fields, degraded gh |

## Integration Tests

- INT-001: PATH-001 (ratio end-to-end incl. direct promote).
- INT-002: PATH-002 (waiver → digest once, idempotent second run).
- INT-003: PATH-003 (sha binding + synthetic rollback surface).
- INT-004: PATH-004 (hostile doc through inventoried collector).

## Phase Test Gates

| Phase | Gate | Command |
|---|---|---|
| 1 (G4+G10) | new pytest + adversary bats green | `python3 -m pytest lib/ tests/ -q && bats tests/bats/adversary-host.bats` |
| 2 (G6+G11) | waiver/lock bats + shellcheck | `bats tests/bats/waiver-record.bats tests/bats/lib-lock.bats tests/bats/run-finalizer.bats && shellcheck -S warning scripts/gsd/waiver-record.sh scripts/gsd/lib-lock.sh` |
| 3 (G7) | binding pytest+bats | `python3 -m pytest lib/tests/test_gates.py -q && bats tests/bats/canary-gate.bats` |
| 4 (G9+G12) | fence/digest bats + skill verify | `bats tests/bats/fence-data.bats tests/bats/credential-guard.bats tests/bats/digest.bats && python3 scripts/verify-skill-blocks.py` |
| Final | cumulative | full pytest ≥577 + full-bats ≥ baseline + shellcheck touched |

Baseline: pytest `lib/ tests/` = **577 passed** @ a8866f7 (measured this
session). Full-bats main-tree count recorded before Phase 1 execution.

## Phase decomposition (feeds spec-decompose)

- Phase 1 — G4 + G10 (REQ-101/102/103, REQ-501): store namespaces, CLI
  subcommands, ratio rule both seams, adversary-host central
  recording+filter+probe, crossing detection + gsd-run consumer, mapping
  record (REQ-703's producer half lands here with gsd-run edits).
- Phase 2 — G6 + G11 (REQ-201/202, REQ-601): waiver recorder + 5 switches
  + plan-wall migration; lock extraction + finisher wait-then-yield.
- Phase 3 — G7 (REQ-301/302): typed canary evidence, sha binding, rollback
  schema+gate.
- Phase 4 — G9 + G12 (REQ-401/402, REQ-701/702/703): reader inventory,
  fence helper + wiring, credential-guard scan mode, digest.sh both modes
  + drift baseline + docs.

## Plan-shape rule (BINDING for decompose — the 006/007 lesson)

Plans carry task sequencing, seam anchors, invariants, and the test list.
The behavioral taxonomy lives in the TEST FILES the tasks create — never
restated as plan prose. Target ≤300 lines per plan. Walls review plans
against THIS spec's REQ/AC tables; the executed diff carries detail.
