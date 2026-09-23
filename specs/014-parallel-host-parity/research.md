# Research and design decisions: spec 014

Date: 2026-09-12. Scope: all five original slices. These are design decisions and observed evidence, not passing implementation gates. Paths below are relative to the isolated FFS root unless absolute.

## R1 — Brownfield implementation and prior art

**Decision:** Extend existing Python/Bash FFS; reuse Git, pinned GSD, stdlib SQLite, and existing dependency constraints. Port narrow lifecycle/locking/adapter ideas only. No competing orchestrator or new runtime dependency.

**Rationale/evidence:** `lib/run_state/state.py:11` already stores global SQLite run state; `scripts/coord/coord.py:623` supplies short registry transactions; `lib/gates.py` supplies grants/evidence. [Prior-art adjudication](prior-art.md) chose port narrow primitives, wrap local executors, build missing FFS durability semantics. Upstream `next` examples are not proof of 1.13 behavior; any actual copied code requires license review/notices.

**Alternatives:** Workstreams alone share source/configuration; adopting worktrunk/workmux/dmux wholesale adds an unrelated orchestration surface and does not supply run/activity/attempt fencing or preserved budgets.

## R2 — Installer containment is an upgrade prerequisite

**Decision:** Before activation, extend the managed installer around explicit runtime layouts, private-stage process/filesystem root mapping, all-destination manifests, hash-guarded backup/rollback, and profile→canary→profile ordering. A private stage resolves every home/config/skill destination inside its bounded child process/filesystem; `--config-dir` alone is never accepted as containment.

**Evidence:** `lib/ffs_installer.py:1598` currently loops both installers without an intervening canary. `gsd_manifest_owned_paths`, `existing_gsd_namespace_paths`, and `install_gsd_with_rollback` now enumerate external Codex skills and preserve rollback snapshots; retain concurrent edits to these functions. [Incident](../../docs/upgrades/2026-09-12-gsd-shared-skills-incident.md): 72 shared skills changed; 12 old-hash-proven files restored, 60 staged files preserved, none deleted. That was the historical mixed state, not a clean rollback. The later version/activation ledger records both profiles upgraded to1.13,866/868 verified entries,72 shared skills reconciled, and59 divergent prior skills preserved.

**Adjudication:** The later [activation analysis](../../docs/upgrades/2026-09-12-gsd-activation-plan.md) supersedes the earlier claim that all 300 Claude-path matches are functional Codex leaks: it reports zero matches in emitted 72 skills/agent TOMLs and matches in copied source catalogs. Treat source warnings as diagnostic; enforce a gate on actual emitted/transitively executed runtime surfaces with a real Codex-only smoke test. Do not merely silence warnings or blindly rewrite source catalogs. Fresh managed activation has reconciled the managed set; unknown ownership/concurrent modifications remain protected and recorded. The successful private canary used a verified `os.homedir()` preload plus macOS sandbox/private GSD/TMP/config roots, without setting HOME/CODEX_HOME; preserve that tested containment strategy and add equivalent Ubuntu proof rather than requiring home-variable rewrites.

**Alternatives:** Copying a prior stage to live roots is nonportable; guessed 1.11 reconstruction cannot restore 60 unproven prior files. No new approval stop is inferred; upgrades were authorized, but containment/source/ownership gates are mandatory.

## R3 — Canonical context and one control authority

**Decision:** Add `lib/run_context.py` for side-effect-free resolution/validation and extend `lib/run_state/state.py` into the supervisor control store, with new importable ownership/workspace/supervisor/migration modules. Keep existing CLI façades in `scripts/coord/coord.py`, `lib/gates.py`, and `lib/run_state/cli.py`; after migration, mutations use one transactional authority. Do not create separate new authoritative ownership and grant files.

**Evidence:** Current RunStore supports only feature/fix, UUID-derived 12-character IDs, and run-level events. `lib/gates.py:2473` anchors one evidence JSON to the primary checkout; `gsd-run.sh` independently derives run directories and activity paths. These need explicit legacy mappings and shared resolution rather than more fallback chains.

**Storage decision:** New authority defaults to a private OS-user state root outside repositories, resolved once by the trusted supervisor (`~/.local/state/feature-fix-swarm` unless a validated operator-supplied state root is configured). Existing `~/.claude/state/runs.db` and repository control JSON remain preserved legacy sources. Use one SQLite database, `BEGIN IMMEDIATE`, foreign keys, `synchronous=FULL`, bounded busy handling, and short transactions. Start with rollback-journal mode; the two-run correctness requirement does not justify WAL/checkpoint complexity. SQLite allows multiple readers but only one simultaneous writer; start the write transaction before checking uniqueness. [SQLite transaction documentation](https://www.sqlite.org/lang_transaction.html).

**Alternatives:** Separate JSON authorities cannot atomically admit run/workspace/objective ownership together. A long SQLite transaction around agent work would serialize runs. Network filesystems/distributed ownership are unsupported; SQLite WAL additionally requires same-host shared memory, so it is not a distributed-lock alternative. [SQLite WAL documentation](https://www.sqlite.org/wal.html).

## R4 — Workspace preparation and crash recovery

**Decision:** Reserve run/objective/workspace intent in the control transaction; release its lock; create Git workspace under a registered sibling workspaces root with a short Git-administration lock; verify registration and finalize reservation. Proposed default is a sibling `<repository-name>-runs/<run-id>` root, outside the primary checkout. Distinct refs are `ffs/runs/<run-id>`. No automatic commit or landing occurs.

**Evidence:** `gsd-run.sh:724` currently creates a detached worktree under primary `.claude/worktrees`, and later planning sync can write workspace planning back to primary (`planning_sync_copy`/`check_planning_divergence`). Replace automatic primary writes with selected-input snapshotting and explicit integration. All skills/plan walls/stateful preflight enter only after workspace-ready; upstream project/workstream/session resolution runs inside that workspace.

**Launch protocol:** Persist attempt/budget debit/fenced intent before process creation. Child starts behind a local handshake, supplies full process identity, and cannot enter the host until supervisor validates generation and records acknowledgement/release. Recovery consults actual process identity and reservation state; unknown liveness blocks, known live child is reconciled, proven dead child may get a separately budgeted attempt. Never repeat an intent to obtain fresh budget. Ordinary model-side loops cannot authorize control changes.

**Alternatives:** Holding a lock for the full run prevents concurrency. Spawn-then-record permits duplicates. Heartbeat expiry/output age/PID alone cannot establish death.

## R5 — Process identity and supported platforms

**Decision:** Centralize process identity behind `lib/process_identity.py`; store host/boot identity, PID and process-start token. Return LIVE, DEAD, or UNKNOWN. Verify existing filelock-backed behavior on installed macOS/Ubuntu before reusing it; wrap private dependency APIs behind this public FFS boundary with a fail-closed fallback.

**Evidence:** `scripts/coord/coord.py:99–153` imports private `filelock._identity` and already has three-valued liveness; `_is_reclaimable` also treats missing worktree as reclaimable, which must not override live/unknown process identity. Existing `scripts/gsd/lib-lock.sh` and credential/install locks need the same durability/identity contract. Requirements already permit `filelock>=3.30,<4`; the invalid 3.29 baseline run is not accepted evidence.

## R6 — Host capabilities and runtime bundles

**Decision:** One versioned capability manifest plus Python loader in `lib/host_capabilities.py` feeds installer doctor, runner, probes, and both runtime builders. Version eligibility is necessary but readiness requires recorded behavioral capability results bound to binary/runtime/config hashes. Establish the current-Codex compatibility slice in M1 without building concurrency; broaden both-host isolation in M5 after M2.

**Evidence:** [Version verification](../../docs/upgrades/2026-09-12-version-verification.md) records Claude 2.1.269/Codex 0.154.0 as current on the check date. `gsd-run.sh:1222` and `ffs_installer.py:2351` duplicate the obsolete Codex <0.148 ceiling. `scripts/gsd/codex-runtime-bundle.py` already verifies/stages hooks and bundles; extend its mechanisms to Claude through a shared importable builder while retaining wrapper compatibility.

**Strict Claude decision:** Build trusted explicit configuration, exclude ambient sources, preserve existing subscription auth through a bounded credential adapter, and prove tool restrictions independently. Managed policy is inspected; unknown execution-widening policy refuses preflight. Documentation establishes settings precedence and that the sandbox concerns Bash; it does not establish a guarantee over every native tool. Actual installed-host CLI/help and canaries decide the supported launch flags. [Settings](https://code.claude.com/docs/en/settings), [sandboxing](https://code.claude.com/docs/en/sandboxing).

**Alternatives:** Widening a version constant alone is insufficient. Reusing shell-only sandboxing as native-tool protection is invalid. Runtime drift cannot silently rewrite a resumable tuple.

## R7 — Routing/review and bounded audit

**Decision:** Keep the spec's exact native model table; canonical model request catalog remains `templates/model-requests.json`, consumed through `lib/model_requests.py` and shell adapters. Review artifacts without producer reasoning; opposite vendor preferred, distinct-model fallback labeled degraded, no automatic frontier escalation. Whole-FFS audit fixes confirmed critical/high and dispositions all other findings before a bounded five-resolver-family refactor.

**Evidence:** `lib/dispatch.py`, `scripts/gsd/model-equivalents.sh`, `model-fallback.sh`, `review-tier.sh`, and `adversary-host.sh` are existing routing surfaces. Current runner wall comments permit HIGH residuals; the requested final/upgraded-baseline gate cannot inherit that exception. Preserve an explicit distinction between wall advisory mechanics and final required readiness.

## R8 — Migration and OpenClaw

**Decision:** Reader compatibility → real-state dual-read → safe legacy handoff → new writer epoch. Migration activates only under a short legacy coordination transaction after proving old owners released/dead; no old/new writers share an authority. New writers are developed/tested only against fixtures before M8 activation. Keep source snapshots, idempotent per-record journal, conflict quarantine, and rollback that freezes writers before changing epoch while retaining evidence.

**Evidence:** `lib/ffs_installer.py:2482` currently reconciles selected scripts with fork allowlist skips; expand to a complete ownership/hash manifest and explicit adjudication, never unconditional skipping. `scripts/gsd/sync-drift-check.sh` requires an explicit canonical source for meaningful consumer checks. Consumer-owned skill trees and user-global guidance remain intact. Actual OpenClaw roots/allowlist contents are inventory outputs, not guessed names.

## R9 — Test and provider applicability

**Decision:** Keep Python/Bats CLI contracts; independently authored acceptance/fault tests precede implementation. No browser stubs. Hermetic suites forbid network; authenticated host canaries are separate opt-in rollout evidence. Enforce full-suite first-party line coverage ≥80%, separate branch coverage, and only vendored/generated exclusions.

**Evidence:** Existing CI uses `python -m pytest lib/ tests/ -q`, separately named contract tests, Bandit, shellcheck, skill/model/environment checks, and recursive Bats discovery. Existing first-party coverage is76.12% line/54.70% branch, with five initial Python failures and corrected Bats still pending (latest778pass/1fail). Full upgraded suites/coverage remain pending despite completed profile/Python/npm/manager component checks. The imported constitution describes Spec Kit's Typer/Windows package rather than this FFS tree; preserve its applicable safety/test/CLI principles without introducing Typer, Windows scope, or a foreign src layout. Record this applicability interpretation in plan.md.

**Provider adaptation:** Spec Kit 1.0.6 setup-plan ran once. Its optional `update-agent-context.sh` extension is absent; no fabricated script, agent-file rewrite, or auto-commit. The plan's context and quickstart provide the required handoff. This is not an unresolved product question or permission request.

## R10 — Fresh review admits repair, not unresolved-defect rollout

**Decision:** M2 requires completed fresh review/adjudication before concurrency code, closes upgrade/admission prerequisites before the affected automatic launch, and assigns accepted M3–M6 findings to owners/regressions/fix phases while retaining them as rollout blockers. Otherwise B1 (workspace-before-writes) would prohibit implementing M3/M4, its required remedy. Parent explicitly adjudicated this dependency-cycle correction. Two fix rounds trigger escalation/written adjudication, not abandonment or waiver. Final zero-critical/high gate remains unchanged.

**Evidence:** The upgraded review names B1–B5 with concrete regressions; the parent has synchronized canonical M2/spec FR-010/AC-010/PATH-003 wording (canonical SHA-256 `a7f38056a36e8e5ae5a00697784d71ef92c16d614f832a4f396ce6a6aaa26889`). No finding is asserted fixed by planning. Known-safe isolated repair work does not authorize ordinary launches through an unproved host envelope.

## Research closure

No product/architecture clarification remains. Empirical installed-host behavior, corrected final Bats results, final coverage, upstream issue status, and live migration/OpenClaw readiness remain required execution gates with named owners/phases in plan.md; they are not asserted as passing by research.
