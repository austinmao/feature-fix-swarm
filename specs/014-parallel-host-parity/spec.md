## Historical evidence closure amendment (2026-09-17)

The operator has adjudicated the six unavailable historical-evidence selectors
and five stale-reference identities as unrecoverable. Their recorded outcomes
remain `RETAIN_UNMET` and `RETAIN_UNMET_UNMAPPED`; they may not be relabelled,
synthesized, or cited as proof that the historical rollout passed. They are
terminal incident-history records and do not independently block completion of
the current Spec 014 implementation. This amendment supersedes any conflicting
historical-completeness prerequisite in AD-016 while preserving AD-016's facts
and anti-fabrication boundary.

Current completion requires fresh, reproducible evidence from the exact source
closure: green Python and Bats suites (with the six retained historical
selectors recorded as strict expected failures), at least 80% first-party line
coverage with branch coverage reported separately, the required concurrency and
isolation proofs, and authenticated both-host canaries. All other readiness,
security, model/provenance, migration, installation, and activation gates remain
in force. The exact terminal dispositions are recorded in the
[acceptance ledger](../../docs/upgrades/2026-09-16-spec014-acceptance-ledger.md)
and [row-10 input disposition](../../docs/upgrades/2026-09-17-spec014-row10-input-disposition.json).

## Recovery target amendment (2026-09-14)

The selected qualification target is GSD **1.14.0**, commit
`f8542fef67c1f978ffa70912cb6f2aaab76464c6`, superseding the historical
1.13 target references below. Original requirement IDs and readiness gates
remain required. Current evidence and unmet work are mapped in
[the recovery continuation](recovery-20260914.md); active installations remain
unchanged during qualification.

## AD-016 sequencing amendment (2026-09-12)

The authoritative amendment is [AD-016](../../docs/upgrades/2026-09-12-ad016-current-reference.md) (repository path `docs/upgrades/2026-09-12-ad016-current-reference.md`). It supersedes conflicting historical-completeness prerequisites below. The eight sealed historical gaps remain UNMET; `full_baseline_complete` and historical comparison PASS are not redefined or asserted. Captured original facts remain mandatory comparison evidence. After terminal upgrade decisions and complete current observations, a separately labeled observed-upgraded-reference may be frozen for fresh M2 review. No concurrency source precedes M2; no final readiness, security, coverage or authenticated gate is waived. See the sealed `docs/upgrades/2026-09-12-historical-audit-limitations.json` enumeration.

# Feature Specification: FFS Upgrades, Parallel Runs, and Host Parity

**Feature Branch**: `014-parallel-host-parity`

**Created**: 2026-09-12

**Status**: Specified; implementation and rollout gates remain unproven

**Input**: Upgrade existing FFS tooling first, review the upgraded baseline, implement automatic isolation for complete parallel FFS runs, establish Claude/Codex parity, audit and verify FFS, then migrate control state and reconcile FFS tooling in OpenClaw. All five original requirement slices are consumed; sources are recorded in the Scope ledger.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Upgrade with a recorded recovery path (Priority: P1)

As the operator, I can upgrade managed profiles and compatible existing tools while current sessions continue, with evidence of every change and recovery instructions for old runtimes.

**Why this priority**: Concurrency work must target an upgraded, reviewed baseline.

**Independent Test**: Upgrade two disposable profile installations with live-session fixtures and preserved customizations; compare evidence and rehearse rollback.

**Acceptance Scenarios**: US1-S1 successful staged upgrade; US1-S2 incompatible upgrade. AC-001–AC-010.

### User Story 2 - Isolate work before planning writes (Priority: P1)

As an operator starting feature, fix, uplift, or FFS-managed GSD work, I receive an isolated workspace before planning, evidence, or stateful preflight writes.

**Why this priority**: Execution-only isolation leaves planning sessions able to overwrite one another.

**Independent Test**: Start different objectives simultaneously from a dirty primary checkout; verify separate planning/source changes and unchanged primary/unrelated inputs.

**Acceptance Scenarios**: US2-S1 parallel planning; US2-S2 failed workspace preparation. AC-016–AC-019.

### User Story 3 - Continue one durable objective (Priority: P1)

As the operator, I can plan, execute, review, and explicitly resume an objective under one run identity using existing syntax and machine-readable results.

**Why this priority**: Per-command identities and newest-result selection lose continuity or resume the wrong work.

**Independent Test**: Exercise anonymous/explicit starts, idle progression, successful-result reuse, explicit revision, unfinished activity, and ambiguous resume.

**Acceptance Scenarios**: US3-S1 activity progression; US3-S2 ambiguous resume. AC-011–AC-016.

### User Story 4 - Admit exactly one owner for contested work (Priority: P1)

As the operator, I can run unrelated objectives concurrently while competing launches for one run, workspace, or objective admit one owner.

**Why this priority**: Removing the repository-wide lease must preserve duplicate-execution protection.

**Independent Test**: Release competing launchers from deterministic hold points; repeat with paused owners, expired heartbeats, reused process IDs, and revoked ownership on both platforms.

**Acceptance Scenarios**: US4-S1 independent objectives coexist; US4-S2 uncertain owner blocks takeover. AC-020–AC-023.

### User Story 5 - Recover without losing evidence or duplicating work (Priority: P1)

As the operator, I can inspect a run from any registered checkout, recover interrupted launches, and finalize workspaces without losing evidence or changing sibling runs.

**Why this priority**: A launch crash must not create a second child or fresh budget; workspace removal must preserve the audit record.

**Independent Test**: Inject spawn-boundary crashes, inspect external registrations, recover from another checkout, and finalize one run while its sibling remains active.

**Acceptance Scenarios**: US5-S1 durable evidence and cleanup; US5-S2 spawn acknowledgement crash. AC-024–AC-028.

### User Story 6 - Use the requested native host and model (Priority: P1)

As the operator, I can select a host, native tier, or exact model and see honest implementation/review provenance.

**Why this priority**: Silent substitution changes the request and can misrepresent review independence.

**Independent Test**: Exercise all native tiers, both review directions, unavailable exact requests, and permitted distinct-model fallback.

**Acceptance Scenarios**: US6-S1 native routing; US6-S2 unavailable exact model. AC-029–AC-032.

### User Story 7 - Launch and resume with verified host behavior (Priority: P1)

As the operator, I can launch authenticated work using a known runtime and trusted configuration, with rejection of unapproved drift or execution-widening policy.

**Why this priority**: Installed files and accepted versions do not prove effective controls or repeatable resumes.

**Independent Test**: Run authenticated canaries for configuration, hooks, shell/native tools, skills, credentials, and altered runtime/configuration identities.

**Acceptance Scenarios**: US7-S1 trusted authenticated launch; US7-S2 unapproved drift. AC-032–AC-035.

### User Story 8 - Resolve audit findings before readiness (Priority: P1)

As the operator, I receive a whole-FFS audit with regression evidence, fixes for confirmed severe defects, bounded refactoring, and producer-distinct verification.

**Why this priority**: Empty reviews and unresolved severe defects cannot establish readiness.

**Independent Test**: Review seeded findings and empty-output fixtures; verify regression ordering, dispositions, provenance, and refusal to pass unresolved critical/high findings.

**Acceptance Scenarios**: US8-S1 verified fixes/refactor; US8-S2 blocking or empty review. AC-010, AC-036–AC-038.

### User Story 9 - Prove the full verification matrix (Priority: P1)

As the operator, I can distinguish fixture evidence from authenticated host evidence and inspect every required concurrency, security, installation, platform, and coverage result.

**Why this priority**: A green subset or missing-auth skip does not prove the supported footprint.

**Independent Test**: Collect the required matrix on macOS/Ubuntu, then remove one mandatory result to verify failure.

**Acceptance Scenarios**: US9-S1 complete evidence; US9-S2 missing authentication/evidence. AC-039–AC-047.

### User Story 10 - Migrate without stealing live ownership (Priority: P1)

As the operator, I can migrate legacy state after compatibility and ownership checks, restart interruption, and roll back without lost evidence or mixed writers.

**Why this priority**: Dependency installation and ownership migration have different safety conditions.

**Independent Test**: Migrate live/dead/conflicting legacy records; interrupt, restart, and rehearse rollback.

**Acceptance Scenarios**: US10-S1 restartable migration; US10-S2 live legacy owner. AC-048–AC-050.

### User Story 11 - Reconcile OpenClaw from verified ownership (Priority: P1)

As the operator, I can update FFS tooling in an isolated OpenClaw integration workspace, preserve consumer customizations, and require real-host evidence before activation.

**Why this priority**: Every fork and owned installation surface must be reconciled without overwriting unrelated skills.

**Independent Test**: Reconcile a consumer fixture with ownership/hash manifest and allowlist; inject unresolved forks/byte mismatches; verify canaries before activation.

**Acceptance Scenarios**: US11-S1 owned-surface reconciliation; US11-S2 unresolved or mismatched surface. AC-051–AC-055.

### User Story 12 - Operate from accurate guidance (Priority: P2)

As the operator, I can follow current setup, host, run, recovery, migration, and rollout guidance with accurate isolation limits and preserved guidance layers.

**Why this priority**: The resulting workflow needs usable installation and recovery instructions.

**Independent Test**: Walk CLI examples against fixtures; check every required topic and host tier; compare repository and user-global guidance.

**Acceptance Scenarios**: US12-S1 usable guidance; US12-S2 misleading guidance. AC-056–AC-060.

## Edge Cases

- **EDGE-001**: A live session rereads upgraded instructions or hits resume drift: preserve its prior identity/recovery path and keep it running; never promise zero downtime.
- **EDGE-002**: An upgrade changes control-state formats: compatibility readers precede the profile flip without granting authority to rewrite live ownership.
- **EDGE-003**: Invalid IDs or truncation-colliding legacy IDs: reject or explicitly map; never truncate silently.
- **EDGE-004**: Inherited run/workspace/session context belongs elsewhere: validate repository/ownership binding before writes.
- **EDGE-005**: Idle, unfinished, completed, revised activity: advance, require explicit resume, reuse result, or record explicit revision respectively.
- **EDGE-006**: Multiple resume candidates: list them, start none, require explicit selection.
- **EDGE-007**: Dirty input includes unrelated source/shared context: snapshot selected inputs/required context only; preserve primary planning.
- **EDGE-008**: Different objectives edit identical relative paths: preserve separate workspaces and branch/ref namespaces.
- **EDGE-009**: Expired heartbeat, paused owner, uncertain liveness, reused PID: expiry alone never authorizes takeover; full process identity matters.
- **EDGE-010**: Revoked ownership or late reserved child: reject stale effects and preserve budget accounting.
- **EDGE-011**: Crash before/after spawn or before acknowledgement: reconcile recorded intent and actual owner identity before replacement.
- **EDGE-012**: External child worktree or removed workspace: use canonical registration; retained evidence remains outside removable worktrees.
- **EDGE-013**: Finalization overlaps sibling work: harvest target evidence first and touch only its manifest-owned resources.
- **EDGE-014**: Unavailable exact model, empty review, missing authentication: report truthful failure/unmet gates; label permitted fallback.
- **EDGE-015**: Ambient hooks/settings or unknown managed policy widen execution: exclude/detect ambient effects and fail unknown widening policy.
- **EDGE-016**: Credentials update during launches: serialize updates without losing credentials or disclosing secret values.
- **EDGE-017**: Legacy conflicts/live leases/interrupted checkpoints: preserve, quarantine, honor ownership, and restart idempotently.
- **EDGE-018**: Review alleges a findings-queue fork or another local incident: inventory/verify before treating it as fact; adjudicate every actual allowlist entry.
- **EDGE-019**: Doctor PASS with wrong manifest source or runtime bytes: source/byte evidence still fails readiness.
- **EDGE-020**: Tool upgrade requires unsupported ranges or loses custom patches: preserve backups and record the concrete blocked upgrade.
- **EDGE-021**: Baseline already fails: preserve actual failures; attributed upgrade exceptions do not become final-gate exemptions.
- **EDGE-022**: Tests need Git history: commits occur only in separate disposable repositories, never linked worktrees of real repositories.

## Requirements *(mandatory)*

### Functional Requirements

Each FR maps to the same-numbered AC. Milestones define required sequencing and evidence, not an implementation plan.

#### Upgrade and upgraded-baseline review — slice 1, M0–M2

- **FR-001**: Before installation changes, record branch, HEAD, dirty paths, worktrees, local/CI results against `origin/main`, full Python/Bats results, every actual failure, first-party line/branch coverage, and the gap to 80% line coverage. Preserve existing work.
- **FR-002**: Back up both shared GSD profiles, managed-runtime staging directories, manifests, configuration, customized integrations, Node build patches, and gstack changes. Stage customization copies separately from active installations before changes.
- **FR-003**: Validate managed manifest `source` fields against canonical FFS, not consumer worktrees; byte-compare staged runtimes with canonical source. Doctor PASS alone is insufficient.
- **FR-004**: Inventory GSD profiles/FFS pin, Claude, Codex, gh, Bats, Playwright, tmux, coreutils, gbrain, Node/npm, and Python; verify current releases and explicitly confirm GSD 1.11.0→1.13.0 as intended target. Verify supplied Claude 2.1.269/Codex 0.154.0 against actual installations/current stable releases; upgrade hosts if newer compatible stable releases exist and record tested versions.
- **FR-005**: Review GSD 1.12/1.13 changelogs for state/lease-format changes before profile flips. Required compatibility readers precede a flip; dependency installation remains separate from later control-state writer migration.
- **FR-006**: Before a profile change, snapshot the old runtime, identify affected live sessions, and record explicit recovery instructions. Do not wait for sessions to finish or terminate them; acknowledge possible instruction rereads/resume drift.
- **FR-007**: Upgrade via managed installer in order first profile → canary → second profile, validating prerequisite compatibility fixes. Failed first-profile canary prevents the second flip until repair or rollback.
- **FR-008**: Refresh compatible npm transitives/Python dependencies without changing the direct dependency set or forcing unsupported GSD ranges; upgrade existing relevant global tools through current managers. Keep Node/npm ownership together, port Node/gstack customizations, and audit external skill pins/compatibility patches before adopting newer commits. Never replace a newer pinned commit with an older release tag.
- **FR-009**: Maintain an upgrade ledger with old/new versions, source, manager, verification, rollback, backup/runtime identities, live-session recovery, and concrete incompatible targets. Re-run and compare the full baseline suite; unexplained new failures block upgrade completion, and baseline-only exceptions require evidence.
- **FR-010**: Before concurrency code, perform fresh upgraded-baseline independent review using artifacts without producer reasoning history. Prefer opposite vendor; label distinct-model fallback degraded and never call self-review independent. Empty/missing output fails closed. Adjudicate every finding before concurrency implementation. Fix upgrade/admission prerequisites before using affected launch paths; findings assigned to the planned M3–M6 repairs receive owners and regression contracts and remain rollout-blocking until resolved. Two unsuccessful fix rounds trigger renewed diagnosis/adjudication, never abandonment or a PASS waiver. Confirmed critical/high findings must be fixed or refuted with evidence before readiness.

#### Context, isolation, ownership, and recovery — slice 2, M3–M4

- **FR-011**: Launchers, skills, gates, status, recovery, and cleanup share one versioned context contract representing run → activity → attempt; one durable objective spans plan/execute/review/resume.
- **FR-012**: Preserve existing syntax and `GSD_RUN_ID`/`GSD_RESUME` compatibility; mint unique anonymous IDs; apply one ID validator and explicit legacy mappings without truncation.
- **FR-013**: Support explicit run selection and JSON operational output. Ambiguous resume lists candidates instead of choosing newest; validate inherited context before use.
- **FR-014**: Context includes repository identity, registered workspace, planning scope, activity/input digest, attempt owner, lease generation, and runtime tuple to distinguish intended work from stale/unrelated state.
- **FR-015**: Idle runs advance planning→execution; unfinished activities require explicit resume; successful activities return their recorded result without rerunning unless explicitly revised.
- **FR-016**: Bind `GSD_PROJECT`, workstream selection, and `GSD_SESSION_KEY` through supported upstream resolution and preserve across activities/attempts. Workstream routing alone does not provide complete source/configuration isolation.
- **FR-017**: Prepare an isolated Git worktree for complete runs before planning seeding, walls, evidence writes, or stateful preflight, for feature, fix, uplift, and FFS-managed GSD activities including planning.
- **FR-018**: Keep planning/source inside the registered workspace. Snapshot selected inputs/required shared context only; never copy unrelated dirty source, overwrite primary planning, or write into the primary/shared checkout. Compare history read-only rather than switching that checkout.
- **FR-019**: Use distinct run branch/ref namespaces; different objectives may edit identical relative source paths concurrently without sibling changes or lifetime repository serialization.
- **FR-020**: Replace the lifetime repository lease with exclusive run/workspace ownership plus duplicate-objective reservation; competing launches for the same run/workspace/objective admit exactly one owner while distinct objectives proceed.
- **FR-021**: Ownership includes nonce, process-start identity, and monotonically increasing fencing generation. Expiry alone never permits takeover; reused PID cannot identify the former owner; uncertain liveness blocks automatic duplicate launch.
- **FR-022**: Validate current ownership on every shared-control mutation. Revoked/stale owners lose authority. Redispatch/orphan reclamation uses full-identity liveness checks, never output-file freshness.
- **FR-023**: Limit shared serialization to short registry, worktree-administration, landing, installation, and credential-sync transactions. Verify atomic-create/rename with fsync durability and platform-specific locking/process identity on macOS/Ubuntu.
- **FR-024**: Partition evidence canonically by durable run independent of invoking checkout; activities/child worktrees resolve the same partition. Global run database and evidence remain outside removable worktrees.
- **FR-025**: Supervisor controls ownership, approval grants, shared coordination, and global run database. Workers write only their own evidence/progress requests and cannot mutate sibling control stores.
- **FR-026**: Retain distinct attempt logs/runtime tuples; enumerate canonical registered runs including external worktrees instead of fixed-directory discovery.
- **FR-027**: Record fenced launch intent before spawn and require reservation acknowledgement; children validate fencing before side effects. Spawn/record crashes cannot duplicate execution or replenish budgets; orphan reservation reclamation requires full-identity liveness proof.
- **FR-028**: Harvest evidence before finalization removal; touch only manifest-owned resources. Planning publication/source landing remains explicit serialized integration subject to existing authorization restrictions.

#### Host parity, routing, review, and audit — slice 3, M5–M6

- **FR-029**: Preserve invoking host, default to Claude if unspecified, and provide the exact mappings below. Frontier is operator-selectable through native tier or exact request.
- **FR-030**: Exact requests never silently substitute. Automatic escalation/fallback stays below frontier, capped at Opus/GPT-5.6 Sol, with actual model/host and degraded provenance recorded.
- **FR-031**: Parallel agents receive explicit non-overlapping ownership; acceptance-test authors differ from implementers. Reviewers receive artifacts without producer reasoning history; opposite-vendor preference and honest fallback/self-review labels apply.
- **FR-032**: Replace duplicated compatibility ceilings with a canonical tested capability contract demonstrating actual installed Claude/Codex behavior, not version acceptance alone.
- **FR-033**: Both hosts launch verified immutable FFS/GSD bundles with runtime/configuration hashes. Resume rejects unapproved tuple drift and reports the mismatch/recovery route.
- **FR-034**: Claude strict launch preserves subscription auth, suppresses ambient user/project settings, explicitly loads trusted configuration, and prevents permission-prompt hangs. Independently control shell networking, native web tools, file tools, and hooks. Unknown execution-widening managed policy fails preflight; shell sandbox alone does not cover all tools.
- **FR-035**: Verify effective Codex hook registration, sandbox behavior, skill discovery, routing, and OAuth synchronization; installed files alone do not establish readiness.
- **FR-036**: Whole-FFS audit follows findings → regression tests → critical/high fixes → behavior-preserving refactor → independent verification; regressions precede corresponding fixes.
- **FR-037**: Bound refactoring to centralizing duplicate path, ownership, host, dependency, and model resolution with evidence of preserved intended behavior outside specified fixes.
- **FR-038**: Finish with zero open confirmed critical/high findings and disposition/owner for every remaining medium/low. Re-pin and verify `REVIEW_GATE_SHA256` if `review-gate-command.sh` changes.

| Responsibility | Codex | Claude |
| --- | --- | --- |
| Frontier planning | GPT-6 Astra, xhigh | Fable |
| Judgment/review | GPT-5.6 Sol, high | Opus |
| Implementation | GPT-5.6 Terra, medium | Sonnet |
| Bounded inventory, documentation, synthesis | GPT-5.6 Luna, low | Haiku |

#### Verification — slice 4, M7 and preceding gates

- **FR-039**: Final full Python/Bats and shell/security/skill/model/docs checks must pass. First-party Python line coverage across the full suite must be ≥80%; report branch separately. Exclude only vendored/generated code, never poorly tested production modules. Baseline failures do not become silent final-gate exemptions.
- **FR-040**: Deterministic hold-point tests plus bounded soak prove Claude/Claude, Claude/Codex, and Codex/Codex overlap, simultaneous planning walls, and same-relative-path edits in separate worktrees. Verify actual wall controls rather than assume environment knobs.
- **FR-041**: Cover same-run/workspace/objective races, inherited environment, anonymous starts, sequential activities, explicit resume, completed-result reuse, and ambiguous selection; each contested identity admits one owner.
- **FR-042**: Inject launch-boundary crashes, stale heartbeat, paused owner, same PID/different start time, quota pause, interrupted migration, and revoked ownership; none corrupt sibling state, duplicate execution, or replenish budgets.
- **FR-043**: Verify both review directions, every native tier on both hosts, exact-request failure, subscription auth, fallback provenance, and resume tuples/drift.
- **FR-044**: Demonstrate actual shell/native-tool denials, trusted/ambient hook canaries, tampering detection, sibling-state protection, Git permission boundaries, and concurrent credential synchronization; verify effects, not configuration presence alone.
- **FR-045**: Verify fresh install, upgrade, collision, partial failure, rollback, and uninstall in user/project scopes on macOS/Ubuntu, including lock/process-identity platform traps.
- **FR-046**: Authenticated real-host canaries are separate from hermetic CI and mandatory for rollout; archive both-host/both-review-direction transcripts. Missing auth or empty/missing gate output records an unmet gate and blocks dependent rollout; fixtures cannot impersonate real canaries.
- **FR-047**: Empirically measure installed upstream worktree parallel execution and record current issue #4588 status; installation/version/workstream documentation alone cannot prove every execution path is parallel.

#### Migration, OpenClaw rollout, documentation — slice 5, M8

- **FR-048**: Separate migration from dependencies; order compatibility readers → dual-read verification against real legacy state → safe legacy ownership handoff → new writers. Honor live leases until release or proof of death; never rewrite live ownership or fabricate PID.
- **FR-049**: Preserve legacy state; import unambiguous records; quarantine conflicts with inspectable reasons; retain imports/quarantines/handoffs in a restartable idempotent journal.
- **FR-050**: Interrupted migration resumes without duplicate imports/ownership changes. Rollback preserves new-format evidence and never mixes old/new writers on the same control state; prove supported writer state in a rollback drill.
- **FR-051**: After FFS gates, reconcile OpenClaw in an isolated integration worktree; scope is FFS tooling, never tenant-service deployment.
- **FR-052**: Adjudicate every actual fork-allowlist entry, including findings-queue if inventory confirms it. Port required adaptations to canonical FFS with regression tests; no skipping or unadjudicated overwrites.
- **FR-053**: A complete verified ownership/hash manifest governs OpenClaw vendored package, root wrappers, pins, coordination helpers, libraries, schemas, and installed host surfaces. Preserve OpenClaw-owned `.claude/skills`/`.codex/skills`; change only proven FFS-owned installed surfaces and explicitly required repo guidance.
- **FR-054**: Before activation, byte-compare staged runtimes to canonical FFS, validate canonical manifest sources, and run `sync-drift-check` with explicit `GSD_SYNC_SRC`; doctor PASS is insufficient.
- **FR-055**: Repeat both-host concurrent canaries against reconciled OpenClaw before local activation; retain transcripts and concrete rollback instructions in the rollout manifest.
- **FR-056**: Update dependencies, setup, host support, all tiers, commands, automatic isolation, evidence, resume/takeover, migration, troubleshooting, examples, and rollout/rollback documentation to match verified behavior.
- **FR-057**: Correct the self-contained project-install claim and name actual shared dependencies; explain linked worktrees as cooperative-development boundaries with shared Git objects, not hostile-worker isolation.
- **FR-058**: Synchronize OpenClaw repo-level Claude/Codex guidance while preserving the separate user-global layer and consumer-owned skills.
- **FR-059**: Preserve existing worktrees/unrelated changes; no unrelated cleanup, tenant deployment, real-repository commit on any branch, push, or release without explicit authorization. Fixture commits use separate disposable repositories. Already-authorized reversible upgrades/staging/implementation/testing/local rollout require no new inferred approval prompts.
- **FR-060**: Attribute readiness claims to evidence and actual runtime/host provenance. Canonical adjudication controls raw Fable review; alleged incidents/failures/forks/manifest weaknesses/environment controls remain hypotheses until independently verified. Missing evidence stays unmet.

### Key Entities *(include if feature involves data)*

- **Run**: Durable objective with repository/workspace/planning scope, branch/ref namespace, and external evidence partition; contains activities.
- **Activity**: Plan/execute/review request with input digest, state, attempts, and reusable successful result.
- **Attempt**: One execution effort with owner, distinct logs/runtime tuple, status, and preserved budget accounting.
- **Ownership record**: Exclusive run/workspace/objective reservation with nonce, process-start identity, fencing generation, and liveness; expiry is not takeover permission.
- **Launch reservation**: Durable fenced attempt/budget intent with spawn acknowledgement sufficient for nonduplicating recovery.
- **Workspace registration**: Canonical run/workspace mapping, input provenance, upstream project/session bindings, and owned-resource manifest.
- **Evidence partition**: Baselines, progress, reviews, logs, tests, migration/rollout records retained independently of removable checkouts.
- **Runtime/capability record**: Actual host/model, immutable FFS/GSD bundle and config identities, and measured behavior governing launch/resume.
- **Upgrade ledger**: Before/after inventory, managers/sources, backups/customizations, verification, incompatibilities, and session recovery/rollback.
- **Finding/review record**: Severity, regression evidence, owner/disposition, reviewer provenance, rounds, verdict, and adjudication.
- **Migration journal**: Legacy sources, imports/quarantines, handoff proofs, writer state, restart checkpoints, rollback evidence.
- **Rollout manifest**: Canonical ownership/hashes, consumer exclusions, fork adjudications, source/byte/drift checks, canaries, activation/rollback.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every upgrade target has before/after outcome, verification, and recovery/rollback; zero existing sessions are terminated or waited out for upgrades.
- **SC-002**: All required host pairings complete overlapping planning/source workloads with zero unintended primary-checkout or sibling changes.
- **SC-003**: Every contested-launch test admits one owner; injected recovery faults produce zero duplicate executions or replenished budgets.
- **SC-004**: All tested activity transitions retain durable identity/evidence; successful repeats reuse results unless revised; all ambiguous resumes require selection.
- **SC-005**: Every exact host/model request runs as requested or explicitly fails; no automatic fallback reaches frontier and no degraded/self review is mislabeled independent opposite-vendor review.
- **SC-006**: All required host-control canaries demonstrate their advertised effect; unapproved drift and unknown widening policy fail before dependent work.
- **SC-007**: Zero open confirmed critical/high audit findings remain; every remaining medium/low has an owner/disposition.
- **SC-008**: The complete automated matrix passes; ≥80% of first-party executable Python lines are covered across the full suite, with separate branch report and justified exclusions.
- **SC-009**: Every rollout-dependent real-host canary has authenticated evidence; missing auth/empty output causes zero passing rollout decisions.
- **SC-010**: Migration preserves legacy evidence, accounts for every imported/quarantined record, respects live ownership, and passes restart/rollback without duplicates or mixed writers.
- **SC-011**: Every OpenClaw fork entry is adjudicated, all owned surfaces match canonical source, and zero consumer-owned skill files are overwritten.
- **SC-012**: All required guidance topics/examples match verified behavior; zero unauthorized real-repository commits/push/releases/tenant deployments occur and unrelated changes remain preserved.

## Assumptions

- **ASSUME-001**: Workers cooperate; worktrees separate source/planning while Git objects stay shared. Hostile-worker isolation is outside the promised boundary.
- **ASSUME-002**: Existing CLI entry points remain the interface. E2E tests exercise terminal/process/filesystem behavior; no browser UI or fabricated selector requirement is in scope.
- **ASSUME-003**: “Keep current dependencies unchanged” preserves the direct dependency set while permitting explicitly authorized compatible refreshes; unsupported changes are individually recorded rather than forced.
- **ASSUME-004**: GSD 1.13.0 is the intended supplied target; “latest” and supplied host versions need verification. A different GSD target is not silently substituted.
- **ASSUME-005**: No waiting applies to profile upgrades; later writer migration still requires released/provably dead legacy ownership and never steals a live lease.
- **ASSUME-006**: The historical 76.12%/54.70% snapshot is superseded as a repository-wide claim by [the coverage correction](../../docs/upgrades/2026-09-12-coverage-correction.md). The corrected denominator discovers the current first-party inventory (34 files at the 2026-09-12 checkpoint) and requires a fresh full run with line coverage ≥80%; branch coverage is reported separately. Preserve actual reports and outstanding Bats/CI evidence; no baseline-green assumption.
- **ASSUME-007**: Producer-distinct fallback review is allowed with degraded provenance when opposite vendor is unavailable; critical/high and empty-output blockers still apply.
- **ASSUME-008**: Two fix rounds apply per finding set before written adjudication, which never waives confirmed critical/high defects.
- **ASSUME-009**: Quarantined conflicts remain inspectable and block their dependent writer/installation changes until resolved rather than choosing an arbitrary winner.
- **ASSUME-010**: Branches/history may be needed for fixtures, but real-repository commits remain prohibited even in run worktrees; use separate disposable repositories.
- **ASSUME-011**: The subsequent technical plan fixes bounded race/soak repetitions/durations before execution and covers every specified hold point/matrix row; no invented throughput target.
- **ASSUME-012**: Five original slices remain scope authority; canonical adjudication adds accepted precision. Rejected raw-review suggestions do not become requirements.

**Dependencies**: Existing managed installers/managers; canonical FFS/OpenClaw ownership records; upstream release/changelog and installed-behavior evidence; macOS/Ubuntu environments; existing host authentication; producer-distinct review; subsequent sequential technical planning. No secret values enter artifacts/transcripts.

**Mandatory order**: M0 baseline/backups → M1 dependencies/tools (compatibility readers first only if changelog requires them) → M2 fresh upgraded-baseline review → M3–M6 context/isolation/host changes and audit → M7 complete verification → M8 safe writer migration and isolated OpenClaw reconciliation/canaries/activation. Documentation follows verified behavior. No concurrency code bypasses M2; no activation bypasses its real-host gates.

## Acceptance Criteria

Each AC verifies the same-numbered FR; authored contracts do not count as passing implementation evidence.

- **AC-001**: Dated baseline contains every required repository/local/CI/suite/failure/coverage field and unchanged preserved-work comparisons.
- **AC-002**: Inventory locates backups of every required surface and separately staged Node/gstack customizations; recovery can read preserved content.
- **AC-003**: Source validation/byte-diff proves canonical identity; deliberately wrong consumer source or bytes fails despite doctor PASS.
- **AC-004**: Ledger lists every tool/profile, dated release checks, actual installed/tested hosts, and explicit intended GSD target confirmation.
- **AC-005**: Pre-flip review records both changelogs' state/lease effects; compatibility-required fixture blocks flip until readers exist and never activates new writers during dependency installation.
- **AC-006**: Old bundles and session identities/recovery precede mutation; live-session canaries remain alive and upgrades do not wait for completion.
- **AC-007**: Evidence orders first profile → passing canary → second profile; failed first canary blocks progression with recorded repair/rollback.
- **AC-008**: Diffs retain direct set/supported ranges, current managers, Node/npm ownership, tested customizations, and audited skill pins; newer-pin-to-older-tag regression is rejected.
- **AC-009**: Every ledger target contains required version/source/manager/verification/rollback/incompatibility fields; repeated suite comparison blocks unexplained new failures.
- **AC-010**: Fresh upgraded review and adjudication predate concurrency code and record reviewer/provenance/verdict/rounds. Missing output fails closed; upgrade/admission blockers prevent affected launches. Planned repair findings have owners and regression contracts and must be resolved before readiness; adjudication cannot waive a confirmed critical/high finding into PASS.
- **AC-011**: Every listed consumer resolves one versioned run/activity/attempt chain across plan/execute/review/resume.
- **AC-012**: Existing syntax/env cases pass, anonymous IDs are distinct, invalid/colliding legacy IDs are rejected or explicitly mapped without truncation.
- **AC-013**: Explicit selection gives valid JSON; ambiguous resume lists candidates/starts none; foreign inherited context fails before writes.
- **AC-014**: Status/export includes every required context field and distinguishes stale from replacement attempts.
- **AC-015**: Tests show idle progression, unfinished explicit resume, completed reuse, and explicit revision without unsolicited rerun.
- **AC-016**: Upstream bindings persist across activities/attempts; concurrent objectives retain separate source/configuration workspaces.
- **AC-017**: All four entry families prepare workspaces before each listed planning/evidence/preflight write; preparation failure causes none of those writes.
- **AC-018**: Primary/unrelated-dirty comparisons remain unchanged; snapshots contain selected inputs/required context only and run writes stay in workspace.
- **AC-019**: Different objectives use distinct branch/ref namespaces and overlap same-relative-path edits without lifetime repository serialization.
- **AC-020**: Same-run/workspace/objective hold-point races each admit exactly one owner while a distinct objective proceeds.
- **AC-021**: Paused/expiry-only cases refuse takeover; reused-PID case rejects stale identity; valid replacement uses new nonce and increased generation.
- **AC-022**: Revoked ownership fails exercised shared-control mutations; stale output/uncertain liveness cannot trigger redispatch or orphan takeover.
- **AC-023**: Both platforms preserve complete durable registry/worktree/landing/install/credential transactions under interruption without lifetime execution locks.
- **AC-024**: Primary/run/child checkout queries resolve one evidence partition; disposable-worktree removal leaves registration and retained evidence readable.
- **AC-025**: Worker sibling ownership/grant/coordination/global mutations fail while own evidence/progress and authorized supervisor transitions succeed.
- **AC-026**: Attempts retain separate logs/tuples; status includes conventional and externally registered workspaces.
- **AC-027**: Pre-spawn/post-spawn/pre-acknowledgement crashes yield at most one child, unchanged budget, stale-fence denials, and full-identity reclamation evidence.
- **AC-028**: Evidence harvest precedes removal; sibling resources remain unchanged; integration uses only the explicit serialized currently authorized path.
- **AC-029**: Eight host/tier cases match exact mappings; unspecified host defaults Claude, invoking/selected host is preserved, operator-selected frontier works.
- **AC-030**: Unavailable exact request fails as requested; allowed fallback records actual identity/degradation and never reaches Fable/Astra frontier.
- **AC-031**: Records show non-overlapping assignments, separate acceptance authors/implementers, reviewer inputs without producer history, and honest provenance.
- **AC-032**: Canonical installed-version capability results show effective behavior; version acceptance alone cannot pass readiness.
- **AC-033**: Launch records immutable bundle/config identities; altered resume fixtures reject before work with mismatch/recovery evidence.
- **AC-034**: Authenticated Claude proves subscription auth, ambient exclusion, trusted config, no prompt hangs, separate tool/hook controls, and rejection of unknown widening policy.
- **AC-035**: Installed Codex proves actual hooks, sandbox denials, skills, routing, OAuth sync; file-presence-only evidence fails.
- **AC-036**: Audit evidence orders findings, failing regressions, fixes, bounded refactor, and producer-distinct verification.
- **AC-037**: Refactor changes map to the five resolver families and preserve expected behavior outside specified fixes.
- **AC-038**: Register has zero open confirmed critical/high and owner/disposition for all medium/low; changed review command matches updated verified pin.
- **AC-039**: Final full suites/checks pass with ≥80% first-party Python line coverage, separate branch report, and only vendored/generated exclusions.
- **AC-040**: Three host pairings show actual overlap/planning walls/isolated same-path edits under deterministic tests and bounded soak, with actual wall controls identified.
- **AC-041**: Each required identity/environment/lifecycle row has evidence satisfying single-owner/unique-start/transition/resume/reuse/candidate-selection expectations.
- **AC-042**: Each named fault has reproducible evidence of no sibling corruption, duplicate execution, or budget replenishment.
- **AC-043**: Results cover both review directions, eight tier/host cases, exact failure, subscription auth, provenance, and accepted/rejected resume tuples.
- **AC-044**: Effective deny/allow tests cover every named security surface including native tools separately from shell and race-safe credentials without secret disclosure.
- **AC-045**: Six lifecycle operations × two scopes × two platforms have result evidence preserving unmanaged data and exercising lock/process identity behavior.
- **AC-046**: Real authenticated canaries are separate from hermetic tests and cover both hosts/review directions; missing auth/empty output blocks dependent rollout.
- **AC-047**: Dated installed-upstream experiment/current issue status records observed parallelism or sequential fallback and its supported-path implications.
- **AC-048**: Journal proves readers → real dual-read → safe released/dead-owner handoff → new writers; live-owner identity remains unchanged without fabricated PID.
- **AC-049**: Legacy records remain preserved and accounted for as imported/quarantined with reasons; repeat checkpoints duplicate nothing.
- **AC-050**: Interrupted restart reaches the supported state; rollback retains new evidence and proves one compatible writer protocol.
- **AC-051**: OpenClaw work uses a registered isolated integration workspace after FFS gates and targets only FFS tooling.
- **AC-052**: Every inventoried fork entry has adjudication, including findings-queue if present; required FFS ports have regressions before adoption.
- **AC-053**: Manifest accounts for all named owned surfaces/hashes; consumer-owned skills remain unchanged and modifications stay in verified FFS surfaces/required guidance.
- **AC-054**: Canonical source/byte checks pass and archived drift invocation shows explicit `GSD_SYNC_SRC`; byte mismatch fails even with doctor PASS.
- **AC-055**: Repeated both-host concurrent canaries predate OpenClaw activation; manifest retains prior runtime identity and usable rollback instructions.
- **AC-056**: Every documentation topic is present and representative CLI examples agree with observed host/run/evidence/resume/migration/rollback behavior.
- **AC-057**: Docs identify shared dependencies, correct self-contained claim, and state shared Git objects/cooperative isolation limit.
- **AC-058**: Repo Claude/Codex guidance agrees with verified workflow; separate user-global layer and consumer-owned skills remain unchanged.
- **AC-059**: Workspace/history/authorization evidence shows no unauthorized real commits/push/releases/tenant deploys/cleanup or discarded changes; fixture commits are separate and authorized reversible work has no invented approval stops.
- **AC-060**: Evidence index separates results, unmet gates, and hypotheses; raw-review claims need independent proof and canonical adjudication controls rejected suggestions.

## BDD Scenarios

Each scenario has exactly one action and stakeholder-observable outcomes.

### US1-S1 — Successful staged upgrade

```gherkin
Given baseline backups and live-session recovery instructions are complete
When the operator performs the managed upgrade
Then the ledger shows a passing first-profile canary before the second profile and existing sessions remain running
```

### US1-S2 — Incompatible upgrade

```gherkin
Given a staged upgrade has an incompatible dependency or failed first-profile canary
When its activation gate runs
Then the next profile stays unchanged and the ledger reports the failure and recovery route
```

### US2-S1 — Parallel planning

```gherkin
Given two different objectives share a repository with unrelated dirty files
When their planning starts concurrently
Then each reports a separate workspace before planning writes and the primary checkout and unrelated files remain unchanged
```

### US2-S2 — Preparation failure

```gherkin
Given an activity has no valid workspace and preparation cannot complete
When the operator starts the activity
Then the command reports preparation failure with no planning, wall, evidence, or stateful preflight writes
```

### US3-S1 — Durable progression

```gherkin
Given a run has completed planning and is idle
When the operator selects it for execution
Then execution retains the run and upstream bindings with a new activity and separately identifiable attempt evidence
```

### US3-S2 — Ambiguous resume

```gherkin
Given two unfinished registered runs match the supplied context
When the operator requests resume without selecting a run
Then the command lists both candidates and resumes neither
```

### US4-S1 — Independent objectives

```gherkin
Given two objectives have distinct registered workspaces in one repository
When their launch requests overlap
Then both are admitted and can edit the same relative path without changing the other's workspace
```

### US4-S2 — Uncertain ownership

```gherkin
Given a claimed run has an expired heartbeat and its owner remains live or uncertain
When another launcher requests takeover
Then takeover is refused with an ownership explanation and no duplicate execution starts
```

### US5-S1 — Durable evidence

```gherkin
Given a completed run and active sibling each have owned resources and retained evidence
When the operator finalizes the completed run from another registered checkout
Then its evidence stays inspectable after removal and sibling resources remain unchanged
```

### US5-S2 — Spawn crash

```gherkin
Given a reserved attempt has a spawned child but its supervisor stopped before acknowledgement
When recovery reconciles the attempt
Then the operator sees at most one executing child with original budget accounting and no stale-owner side effects
```

### US6-S1 — Native routing

```gherkin
Given both hosts are available and a supported native tier is selected
When the operator runs the activity with review
Then the selected host and tier are used and review reports its actual producer-distinct reviewer and provenance
```

### US6-S2 — Exact request unavailable

```gherkin
Given the exact requested model is unavailable on the selected host
When the operator starts the activity
Then the command reports that exact request unavailable without substituting host or model
```

### US7-S1 — Trusted launch

```gherkin
Given a verified runtime and trusted configuration support existing subscription authentication
When the operator launches the strict host canary
Then authentication succeeds, advertised controls take effect, and results identify the actual runtime and configuration
```

### US7-S2 — Drift rejection

```gherkin
Given a resumable attempt has unapproved runtime or configuration drift or unknown execution-widening managed policy
When the operator requests launch or resume
Then preflight rejects it with the mismatch or policy reason before work continues
```

### US8-S1 — Verified fixes

```gherkin
Given an audit has reproducible findings and failing regression evidence
When fixes and bounded resolver refactoring pass independent verification
Then the register has no open confirmed critical/high findings and owner/disposition for every remaining medium/low
```

### US8-S2 — Review cannot pass

```gherkin
Given a review has empty output or unresolved confirmed critical/high findings after allowed fix rounds
When readiness evaluates the review
Then the gate refuses PASS and records missing evidence or blocking findings despite written adjudication
```

### US9-S1 — Complete matrix

```gherkin
Given all required platform, lifecycle, concurrency, routing, security, coverage, and authenticated results are passing
When the operator evaluates the full rollout gate
Then readiness reports the complete evidence index with separately labeled real-host and hermetic results
```

### US9-S2 — Missing evidence

```gherkin
Given a mandatory canary lacks authentication or a required gate produced empty output
When the operator evaluates rollout readiness
Then the specific unmet gate prevents dependent rollout despite passing fixture tests
```

### US10-S1 — Restart migration

```gherkin
Given compatibility and real legacy dual-read checks pass with safe ownership handoffs and a partial migration journal
When the operator restarts migration
Then records import once, conflicts remain quarantined with reasons, and writers activate only after recorded safe handoffs
```

### US10-S2 — Live legacy owner

```gherkin
Given a legacy lease has a still-live owner
When migration reaches its handoff
Then that owner remains unchanged and dependent writer activation awaits release or proof of death without terminating the session
```

### US11-S1 — Reconcile owned surfaces

```gherkin
Given FFS gates pass and every OpenClaw fork and installed surface has verified ownership and adjudication
When the operator reconciles the isolated integration workspace
Then owned surfaces match canonical source, consumer skills stay intact, and activation follows passing repeated both-host canaries
```

### US11-S2 — Incomplete consumer proof

```gherkin
Given an OpenClaw fork is unadjudicated or staged bytes differ from canonical FFS
When the operator evaluates activation
Then activation is refused with the affected entry or mismatch even if doctor reports PASS
```

### US12-S1 — Usable guidance

```gherkin
Given verified CLI behavior and rollout evidence are available
When the operator follows updated setup, run, recovery, migration, and rollback examples
Then commands match observed behavior and explain dependencies, tiers, evidence, and isolation limits
```

### US12-S2 — Misleading guidance

```gherkin
Given draft guidance claims self-contained project installation, hostile-worker isolation, or unauthorized real-repository commits
When the documentation gate evaluates it
Then the gate identifies each false or unauthorized instruction and refuses readiness until corrected
```

## E2E Test Paths

CLI/process/filesystem contracts; fixture commits use separate disposable repositories. The subsequent technical plan supplies unit/integration maps, runnable commands/stubs, and bounded repetition parameters. Report hermetic and authenticated paths separately.

| Path | Critical journey and observable oracle | Criteria | Evidence |
| --- | --- | --- | --- |
| **PATH-001** | Baseline/backups → canonical source/byte checks → inventory; all fields attributable and preserved work unchanged. | AC-001–004, 059–060 | Fixture and actual baseline |
| **PATH-002** | Changelog/readers → live snapshots → profile/canary/profile; failed canary blocks progression without ending sessions. | AC-005–009 | Install fixture/live ledger |
| **PATH-003** | Upgraded artifacts → distinct review/adjudication → repair ownership; empty output blocks implementation, admission defects block affected launch paths, and unresolved critical/high blocks readiness. | AC-010, 031, 060 | Pipeline fixture/fresh review |
| **PATH-004** | Feature/fix/uplift/GSD planning from dirty checkout → isolation; preparation failure leaves pre-planning surfaces untouched. | AC-016–018 | Real CLI in disposable repos |
| **PATH-005** | Anonymous → select → plan/execute/review → reuse/revise/resume; stable context/JSON and distinct attempts. | AC-011–016, 026 | CLI lifecycle fixture |
| **PATH-006** | Invalid/legacy IDs, foreign env, ambiguous resume → explicit rejection/candidates; no truncation or accidental launch. | AC-012–015, 041 | CLI error fixture |
| **PATH-007** | Three host pairings → overlapping planning walls/same-path edits → inspect namespaces/siblings. | AC-017–020, 040 | Deterministic tests/soak |
| **PATH-008** | Same run/workspace/objective hold-point races → exactly one owner; unrelated objective proceeds. | AC-020–023, 041 | Both-platform race logs |
| **PATH-009** | Pause/expire/reuse PID/revoke → takeover/mutation → truthful refusal without duplicate. | AC-021–023, 042 | Ownership fault fixture |
| **PATH-010** | Crash pre-spawn/post-spawn/pre-ack → recovery/stale child → at most one child, preserved budget. | AC-027, 042 | Process-crash fixture |
| **PATH-011** | Query primary/run/external child → enumerate → finalize → evidence survives, sibling unchanged. | AC-024, 026, 028 | Status/cleanup fixture |
| **PATH-012** | Own worker progress/sibling controls → effective allow/deny; concurrent credentials preserve identities. | AC-025, 044 | Supervisor/credential fixture |
| **PATH-013** | All native tiers → unavailable exact request → allowed fallback → identities/ceiling/provenance. | AC-029–032, 043 | Routing tests/real canaries |
| **PATH-014** | Claude→Codex review and Codex→Claude review → inspect reviewer inputs and independent authorship. | AC-010, 031, 043, 046 | Authenticated transcripts |
| **PATH-015** | Strict Claude subscription/config/hooks/shell/native tools/policy → effective controls without hangs. | AC-033–034, 044 | Authenticated strict launch |
| **PATH-016** | Actual Codex hooks/sandbox/skills/routing/OAuth → altered resume → behavior proof and drift rejection. | AC-032–035, 043–044 | Authenticated capabilities |
| **PATH-017** | Findings → failing regressions → fixes/refactor → verification → dispositions/gate pin. | AC-036–038 | Audit/regression/review |
| **PATH-018** | Full suites/coverage → evidence gate → missing auth/output → unmet rollout despite fixture success. | AC-039, 042–046 | Reports/failure fixture |
| **PATH-019** | Six lifecycle operations × two scopes × two platforms → preserve unmanaged data and durable state. | AC-023, 045 | Complete lifecycle matrix |
| **PATH-020** | Installed upstream worktree experiment/current issue status → record actual overlap/fallback. | AC-047 | Dated empirical report |
| **PATH-021** | Real legacy dual-read → live/dead/conflicting handoff → interrupt/restart/rollback → no loss/mixed writers. | AC-048–050 | Migration fixture/real-state proof |
| **PATH-022** | Isolated consumer inventory → every fork adjudication → tested FFS port → owned reconciliation. | AC-051–053 | Manifest/fork/regression records |
| **PATH-023** | Consumer byte/source/drift checks → repeated both-host canaries → activate/rollback; missing proof blocks. | AC-054–055 | Actual checks/real canaries |
| **PATH-024** | Walk docs → compare guidance layers/workspaces/history → reject false claims/unauthorized actions. | AC-056–060 | Docs/authorization evidence |

## E2E Playwright Stubs

This is a CLI-only feature, so these are named Playwright-equivalent terminal/process/filesystem contracts rather than browser tests. They are future stubs: each names real arrange/act/assert work and must not be treated as a passing placeholder or a fake browser test.

```python
def test_path_001():
    # arrange repository and tool estate; act baseline/backup inventory; assert attributable evidence and preserved unrelated work
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_002():
    # arrange staged profiles and live sessions; act profile/canary/profile upgrade; assert ordering, rollback on failure, and sessions remain alive
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_003():
    # arrange independent review artifact; act adjudication; assert empty output and severe findings block
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_004():
    # arrange dirty primary checkout; act feature launch; assert workspace exists before planning writes
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_005():
    # arrange anonymous and explicit runs; act plan/execute/review/reuse/revise/resume; assert stable durable identity and distinct attempts
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_006():
    # arrange invalid/legacy IDs, foreign environment and ambiguous resume; act resolve; assert explicit rejection/candidates and no accidental launch
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_007():
    # arrange overlapping relative paths; act concurrent planning; assert isolated branches and unchanged primary
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_008():
    # arrange competing owners; act simultaneous reservation; assert exactly one winner
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_009():
    # arrange paused, expired and reused-PID owners; act takeover; assert uncertain ownership blocks
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_010():
    # arrange spawn and acknowledgement crashes; act recovery; assert no duplicate child or budget refill
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_011():
    # arrange external checkout and removable workspace; act inspect/finalize; assert canonical evidence remains
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_012():
    # arrange worker and credential races; act writes; assert sibling control is denied and secrets stay redacted
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_013():
    # arrange native host/tier requests; act route; assert exact mapping and invoking host provenance
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_014():
    # arrange independent artifacts for Claude-to-Codex and Codex-to-Claude; act review in each direction; assert distinct authorship, isolated reviewer inputs and actual host/model provenance
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_015():
    # arrange subscribed strict Claude launch; act config/hook/shell/native-tool/policy canaries; assert actual controls and no permission-prompt hangs
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_016():
    # arrange subscribed Codex runtime; act hook/sandbox/skill/routing/OAuth canaries and altered resume; assert actual behavior and rejected drift
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_017():
    # arrange seeded findings and regressions; act audit; assert regression-before-fix and independent verification
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_018():
    # arrange missing matrix/auth evidence; act aggregate; assert failed closed with unmet path
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_019():
    # arrange lifecycle operations on both scopes/platform fixtures; act install/rollback; assert unmanaged data survives
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_020():
    # arrange disposable upstream worktree; act run parallelism experiment; assert observed behavior and issue status
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_021():
    # arrange legacy records and live owner; act dual-read/handoff; assert no writer theft or duplicate import
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_022():
    # arrange OpenClaw fork inventory; act adjudicate; assert every fork has disposition and regressions
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_023():
    # arrange owned-surface manifest and byte mismatch; act reconcile; assert mismatch blocks activation
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
def test_path_024():
    # arrange verified guidance and authorization ledger; act aggregate readiness; assert docs and authorization gates
    raise NotImplementedError("Contract stub; requires actual implementation evidence")
```

## Test Contract Summary

| Contract | Count |
| --- | ---: |
| Original slices consumed | 5 |
| Prioritized stories | 12 |
| BDD scenarios | 24 |
| Functional requirements | 60 |
| Acceptance criteria | 60 |
| Critical CLI/E2E paths | 24 |
| Edge cases | 22 |
| Unit test cases | 58 |
| Unit mapped source/test rows | 16 |
| Integration cases | 16 |
| Success criteria | 12 |
| Assumptions | 12 |

Counts describe authored contracts, not executed results. The CLI applicability adaptation above intentionally supplies no browser selectors or fake passing test files.


## Scope ledger

**Authoritative source**: `docs/plans/2026-09-12-operator-plan.md` (durable byte-identical copy of `/tmp/ffs-parallel-source-20260912.md`).

**Source SHA-256**: `6eff15ae24b33c475f34e4d5592da18674c694ee560c6f0d652ad1b36a19eb59`

**Canonical adjudicated plan**: `docs/plans/2026-09-12-ffs-parallel-host-parity.md`

**Canonical plan SHA-256 at specification**: `a7f38056a36e8e5ae5a00697784d71ef92c16d614f832a4f396ce6a6aaa26889`

Slice hashes cover original bytes from each `## N.` heading through the byte before the next numbered heading; slice 5 extends to EOF including the final restriction. Preamble/operator instructions on lines 1–6 apply to all slices and are retained in FR-010/059. Exactly the original five slices are enumerated; milestones do not replace them.

| Original slice | Lines | Slice SHA-256 | Disposition | Requirements/ACs | Stories/paths | Milestones |
| --- | --- | --- | --- | --- | --- | --- |
| 1. Upgrade first | 7–18 | `137b5c949629c329ade64d2816041866fc3c5885ffafcc64f64398d28099ae2f` | CONSUMED | 001–010, 059–060 | US1/8; PATH-001–003 | M0–M2 |
| 2. Implement automatic parallel-run isolation | 19–49 | `ecc23e0c209d6c1916b7a94e8f805f4f6e9d2fd72477c1351a3c8aefa30cc131` | CONSUMED | 011–028, 057 | US2–5; PATH-004–012 | M3–M4 |
| 3. Host parity, model routing, review, and refactor | 50–69 | `56f442cc45a6cc922d2ae254832beb635ccb7160be6a0b308f69e608d7663063` | CONSUMED | 010, 029–038 | US6–8; PATH-003, 013–017 | M5–M6 |
| 4. Verification gates | 70–81 | `3a8e18155484270acdd6e45fa0c1e012cb34b318614f58a60d884d1729f7ff05` | CONSUMED | 039–047 | US9; PATH-007–010, 013–020 | M7/phase gates |
| 5. Migration, OpenClaw rollout, documentation | 82–91 | `d997080d269cf63ea9291c602cd5ef95713f15c319a638e2755baa5a98c5a015` | CONSUMED | 048–060 | US10–12; PATH-021–024 | M8 |

**DESIGN-DOC COVERAGE: 5 of 5 slices consumed; unconsumed: []**

### Required evidence traceability

| Canonical evidence obligation | Criteria |
| --- | --- |
| 1. Local/CI baseline vs origin/main, failures, coverage, inventory, backups, source/bytes | AC-001–004 |
| 2. Pre-flip 1.12/1.13 state-format changelog review | AC-005 |
| 3. Upgrade ledger, old/live runtime identities, recovery/rollback | AC-006–009 |
| 4. Fresh review identity/provenance/verdict/rounds/adjudication and empty-output refusal | AC-010, 031 |
| 5. Actual host capability/strict-launch behavior | AC-029–035 |
| 6. Races, reused PID, spawn/record crash, expiry without takeover | AC-020–023, 027, 040–042 |
| 7. ≥80% full-suite first-party line coverage, separate branch/exclusions | AC-039 |
| 8. Authenticated both-host/both-direction transcripts and unmet-auth records | AC-043–046 |
| 9. Upstream worktree experiment/current issue #4588 status | AC-047 |
| 10. Migration imports/quarantines/handoffs/restart/rollback | AC-048–050 |
| 11. OpenClaw ownership/hash/fork manifest, bytes/explicit-source drift/canaries | AC-051–055 |
| 12. Documentation diffs, installation reality/isolation limit/guidance layers | AC-056–058 |
| 13. Authorization record/no unauthorized real commits/push/releases | AC-059–060 |

### Grounding references to verify during execution

- [GSD releases](https://github.com/open-gsd/gsd-core/releases): target/current release and 1.12/1.13 state changes before flips.
- [GSD v1.13.0 workstreams](https://github.com/open-gsd/gsd-core/blob/v1.13.0/docs/how-to/work-in-parallel-with-workstreams.md): upstream resolution and routing/isolation boundary.
- [Claude sandboxing](https://code.claude.com/docs/en/sandboxing): effective shell/native-tool controls separately.
- [GSD issue #4588](https://github.com/open-gsd/gsd-core/issues/4588): current status alongside installed-capability experiment.
- [Original source](../../docs/plans/2026-09-12-operator-plan.md), [canonical plan](../../docs/plans/2026-09-12-ffs-parallel-host-parity.md), [raw review](../../docs/plans/2026-09-12-fable-review.md), and [prior-art adjudication](prior-art.md): original scope/canonical adjudication governs; raw historical/local claims require evidence.

### Specification test-contract counts

| Contract | Count |
| --- | --- |
| Original slices consumed | 5 of 5 |
| Prioritized stories | 12 |
| BDD scenarios | 24: happy/error per story |
| Functional requirements | 60 |
| Numbered acceptance criteria | 60: one per FR |
| Critical CLI E2E paths | 24 |
| Edge cases | 22 |
| Success criteria | 12 |
| Assumptions | 12 |

Unit/integration test maps and executable path stubs belong to the next sequential planning/clarification steps. Counts describe authored contracts, not executed results.
