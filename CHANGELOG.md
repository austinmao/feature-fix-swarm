# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
on a per-skill basis. Each skill in `skills/` carries its own version field in
its SKILL.md frontmatter; this CHANGELOG aggregates user-facing changes across
all skills.

## Unreleased

### Added

- `/feature-implement --autonomous` gets a bounded, grant-gated rc-3 auto-continue: on `WALL-ROUND-CAP`/`WALL-NO-CONVERGENCE`, a phase with zero unresolved HIGH/CRITICAL wall findings, an operator `wall-reset:<phase-slug>` grant, and an unspent `PLAN_WALL_AUTO_RESET_MAX` budget (default 1) gets exactly one wall reset + re-run before quarantine goes terminal; enforced in `scripts/gsd/gsd-run.sh` (`_gsd_run_wall_gate` — the actual `--autonomous` runner seam), documented in `skills/feature-implement/SKILL.md`. `/feature-spec` Step 6 MAX-AUTH enumeration now mints one `wall-reset:<phase-slug>` grant per phase directory (autonomy-grant 1.4.0 adds the gate type). Interactive sessions never mint the grant, so their exit-3 stop is unchanged.
- Fix-round mutation contract (feature-implement 2.14.0, plan-decompose 1.7.0): a wall/plan-gate fix round must rewrite or delete the defective plan text — never append a correction block leaving the original standing, which next-round reviewers re-notice and the findings queue counts as REOPEN=NEW, tripping the diminishing-returns rule (root cause of the spec-385 no-convergence quarantines).
- `docs/configuration.md` now documents the previously missing-but-read `PLAN_WALL_TIMEOUT`, `PLAN_WALL_MAX_ROUNDS`, `PLAN_WALL_REASON`, `PLAN_WALL_AWAIT_MAX/POLL/COUNT` knobs plus the new `PLAN_WALL_AUTO_RESET_MAX`.

- `plan-wall.sh --await` enforces `PLAN_WALL_AWAIT_MAX` (default 6): pending returns beyond the cap exit rc 76 `WALL-AWAIT:attempts-exhausted`; the counter is run-scoped, evaluator probes (`PLAN_WALL_AWAIT_COUNT=off`) are budget-neutral, and decided outcomes always report through and reset it.
- `digest.sh --immediate` appends schema-true retro rows to `.feature-fix-swarm/digest-<utcdate>.jsonl` for list-backed event classes — the previously missing producer for `retro.sh analyze` (`DIGEST_RETRO_SINK=off` disables).

### Fixed

- `scripts/gsd/reconcile.sh` reports terminal records (`done`/`failed`/`quarantined`) as `RECONCILE:terminal` instead of mislabeling a just-failed run `still-waiting` — including a record the ci-watch evaluator fails mid-pass; `scripts/gsd/session-wake.sh` typed failure tokens no longer embed the exit code (`SESSION-WAKE:wake-exhausted`, not `wake-exhausted 1`); `plan-wall.sh --await` now recognizes socratic-armed verdict records (folded `plan_sha:socratic_sha` keys) so `wall-decided` wakes fire for socratic-armed phases.

- Consent-gated retro filing now publishes a finite diagnostic allowlist, maintains upstream advisory labels and occurrences, and supports maintainer-only factual triage for human-reviewed specification work.
- `scripts/gsd/reconcile.sh` provides a one-pass, coord-claimed recovery path for durable lifecycle records, and `docs/healing.md` documents its operator controls and recovery boundaries.

### Changed

- `scripts/gsd/gsd-run.sh` retries one failed drive by default (`FFS_RESPAWN_MAX=1`): rc 124 retries once, while other failures retry only after a zero-commit probe. A failed drive whose capture carries a vendor session-limit banner is instead checkpointed as a durable `waiting(time)` record (`FFS_SESSION_WAKE=off` disables) and left for `scripts/gsd/reconcile.sh` to relaunch.

- Dependency refresh (2026-08-08): `@opengsd/gsd-core` exact pin `1.9.1` →
  `1.10.0` (constants in `lib/ffs_installer.py`, `scripts/gsd/stage-gsd-skills.py`,
  `scripts/gsd/codex-runtime-bundle.py`, plus lockfile, fixtures, and docs);
  `pytest` `9.0.3` → `9.1.1`; vendored `socratic` pin `862b52e` → `8c7e1fd`
  (upstream added an opt-in `grades/` readiness-gate surface — additive, inert
  unless a grade is named). `npm audit fix` cleared one high (`ip-address`
  SSRF) and one moderate (`hono`) transitive advisory. The `gsd-manifests`
  doctor message now interpolates `GSD_VERSION` instead of repeating the
  literal, so it cannot drift from the pin again.
- Codex CLI supported range widened to `>=0.137.0,<0.148.0` (was `<0.147.0`),
  admitting the `0.147.x` line. Not a blind ceiling bump: `0.147.0` was
  installed to a scratch prefix and exercised against FFS's exact `gsd-run`
  invocation — `codex exec -c model=… -c model_reasoning_effort=… --sandbox
  workspace-write --color never` — inside an *unfamiliar* fresh git project,
  the precise condition upstream's new explicit-project-trust requirement
  (openai/codex#36960) would have rejected. The drive returned `PROBE_OK`
  with rc 0, and `--version` still parses. `0.147.0` also removes
  `codex exec --full-auto`, which FFS has never used (it already passes
  `--sandbox workspace-write`, the documented replacement). Both range sites
  moved together — `CODEX_MAX_VERSION` in `lib/ffs_installer.py` and the
  independent `version_in_supported_codex_range` predicate in
  `scripts/gsd/gsd-run.sh` — plus their two out-of-range test fixtures.

### Fixed

- `setup.sh`/`ffs_installer.py` hard-blocked the *entire* install whenever a
  single managed skill path had an edited/unmanaged collision, with no way to
  proceed for the rest of the skills. New `--adopt-collisions` flag backs up
  the colliding path (printing the backup location) and installs the vendor
  copy for just that path, while every other skill installs normally. Default
  behavior (hard-block, fail-closed) is unchanged unless the flag is passed.
- `tests/bats/socratic-enum-drift.bats` no longer fails on upstream's
  `packs/_template` scaffold, which ships a `core.md` but is deliberately
  absent from `PACK_ENUM`. The suite is opt-in (`FFS_SOCRATIC_DIR`), so it had
  never been exercised in CI; it failed identically against both the old and
  new socratic pins, confirming a suite bug rather than vendor drift.

### Added

- Post-spec-005 hygiene: opt-in enum-drift suite
  (`tests/bats/socratic-enum-drift.bats`) asserting
  `DOMAIN_ENUM_ORDER`/`PACK_ENUM` map 1:1 to a REAL resolved socratic vendor
  tree (skips where none resolves, so CI is unaffected); spec-005 retro note
  (`specs/005-socratic-integration/retro.md`).

### Changed

- `lib/ffs_installer.py`: `stage_prompt_master`/`stage_socratic` deduplicated
  into one `_stage_external_skill()` with a uniform env contract —
  prompt-master gains `FFS_PROMPT_MASTER_INSTALLER` for parity with the
  socratic seam; `legacy_skill_names()` now recognizes a stray legacy
  `socratic` dir as managed during migration.

### Fixed

- `scripts/gsd/plan-wall.sh`: fixed the schema-validator defect (rc=97) that
  forced every WAIVED verdict in the spec-005 run — the vendor clause in
  `_pw_validate_findings` piped into the enum array without an `as` binding,
  so `.vendor` indexed the enum array and jq errored, rejecting EVERY finding
  that carried the prompt-mandated `vendor` key as schema-invalid. Fixture
  stubs never set `vendor`, so only real model output hit it. Regression
  tests cover the full-shape finding and the out-of-enum vendor rejection.
- `scripts/install-prompt-master.sh`: `--dest`/`--source` with a missing
  value now exits 2 with a usage line (previously died via failed `shift 2`
  under `set -e`), matching `install-socratic.sh`.

- Added a fourth model-request tier, `frontier` (spec 004,
  `specs/004-model-routing/`), splitting the collapsed `judgment` tier that
  planning and review previously shared. `gsd-planner` moves to `frontier`
  (Claude Fable / Codex Sol @ xhigh); `gsd-plan-checker` and other
  review/verification roles stay on `judgment` (Claude Opus / Codex Sol @
  high) — the two now resolve to genuinely different models on Claude, and to
  a different effort on Codex, closing a same-model self-review gap in the
  plan gate. Added an always-on per-phase plan wall
  (`scripts/gsd/plan-wall.sh`, kill-switch `PLAN_WALL=off` with a durable
  waiver) that dispatches an adversarial review of each phase's plan to a
  model distinct from the planner's before that phase can execute, selected
  by a diversity-invariant algorithm (cross-vendor beats same-vendor
  different-model beats same-model different-effort); a completion backstop
  in `scripts/gsd/gates-test-command.sh` additionally refuses to let a phase
  finish without a passing wall record. `findings-queue` (`lib/gates.py`)
  gained typed dispositions (`refute|fix|waive` with a required reason) and
  reopens a finding whose signature recurs after resolution. Added
  `SECURITY_MODEL_FENCE=off` as an explicit kill-switch for the existing
  security-spec planning fence.
- Added `/git-branch-consolidate` v1.0.0 and `/git-branch-cleanup` v1.0.0, a
  repository-hygiene pair for reconciling a sprawling branch estate back down
  to one `origin/main`. Consolidate is read-only: its deterministic collector
  (`skills/git-branch-consolidate/scripts/collect-estate.py`) classifies every
  branch and worktree by whether the base branch already carries the content,
  what work is still owed, whether tests exist, and whether the
  spec/plan/tasks trail is complete, then writes an ordered merge set, cleanup
  set, and testing-gap list to `.planning/ESTATE-<UTC>.md` without ever
  checking out, fetching, merging, or deleting. Cleanup owns the acting half:
  CI-gated merges of non-gated PRs under the pinned merge protocol, then
  pruning merged refs and their worktrees. Both treat merged-PR state and
  residual-code emptiness as authoritative rather than `git branch --merged`,
  which reports every squash-merged branch as unmerged; remote deletion is
  compare-and-delete via `--force-with-lease` only. Consolidate hands its
  `delete-safe` set to cleanup and never the reverse — land before cleanup, or
  you delete the only copy.
- Added `/spec-guide` v1.0.0 to generate developer, admin, and user
  instructions for a delivered spec and verify every step through its actual
  browser, API/MCP, chat, email, CLI, webhook, worker, database, or design
  surface. Its deterministic collector inventories source and evidence names
  without reading evidence contents, and the installer ships the skill to both
  Claude and Codex discovery roots.
- Added a pinned `socratic` question bank (spec 005,
  `specs/005-socratic-integration/`), vendored via `vendor/socratic/pin.json`
  and `scripts/install-socratic.sh` and staged by `stage_socratic()` into
  `.agents/skills/socratic` as a managed external skill with fingerprint
  tracking, skippable with `FFS_SKIP_SOCRATIC=1`. Added
  `scripts/gsd/socratic-slice.sh`, the single deterministic emitter that
  turns a spec's `socratic.md` frontmatter into a delimited, domain-scoped
  slice, with two deliberately different postures: fail-soft at consumption
  time, so a missing vendor tree or an unknown domain degrades to a thinner
  slice, and fail-closed under `--validate` at authoring time, so an invalid
  spec is rejected outright. `/feature-spec` Step 1.5 now authors
  `specs/NNN/socratic.md` with zero operator prompts, routing open questions
  to typed `gates.py` PENDING actions rather than into the MAX-AUTH
  auto-grant enumeration. Three reviewer seams are armed by the resulting
  slice — `plan-wall.sh`, `plan-decompose` Step 3, and `review-gate`'s honest
  verifier, the last of which audits each ASSUME entry to held, violated, or
  unverifiable and routes violated entries into the normal findings queue at
  HIGH. Every seam is fail-soft: with no vendor tree installed, each prompt
  stays byte-identical to its pre-feature form, and `SOCRATIC=off` disables
  arming for a single run through that same path.

### Fixed

- `/spec-status NNN` now resolves the unique `spec-NNN` archive when the active
  `.planning/` tree carries several *unrelated* identities. The conflicting
  active identities check previously fail-closed on any multi-identity tree,
  which blocked status for an explicitly-requested archived spec whenever the
  root project had moved on. A conflict that includes the requested spec still
  fails closed — active planning must not be ambiguous about the spec you
  asked for. Regression test covers the live shape that exposed this: a
  `.planning/` root naming two other specs while `spec-NNN` sits in the
  archive.

### Security

- Updated the pinned pytest development tool to 9.0.3 after GitHub's
  post-merge dependency refresh identified GHSA-6w46-j5rx-g56g in 9.0.2.
- Preflight probes now require a structured `argv` array and execute without
  an implicit shell. Legacy `cmd` strings and environment placeholders fail
  closed; migrate each command to one literal argument per JSON array item and
  let invoked programs read secrets from their inherited environment.
- Remediated one high and two moderate transitive npm advisories while keeping
  the direct GSD 1.9.1 pin exact.
- CI actions are immutable-SHA pinned with least-privilege permissions; CodeQL,
  Bandit, OpenSSF Scorecard, and Dependabot coverage were added.

### Documentation

- Reframed the README around audience, problem, outcome, quick start, and the
  GSD/FFS ownership boundary; added dependency, security, contribution,
  support, conduct, issue, and pull-request guidance.
- Removed stale internal GSD planning state and superseded Ruflo spike output
  from the public source tree. Reproducible model-evaluation evidence and
  test-referenced specifications remain tracked.

## v5.0.5 — Durable finalization and archive-aware status (2026-08-01)

### Fixed

- Finalization now copies each worktree's run ledger to a durable, run-keyed
  archive before removing the worktree. The copy is containment-checked,
  symlink-safe, checksum-verified, concurrency-safe, and complete-or-refuse;
  an external in-worktree `GATES_STORE` is preserved alongside the standard
  `.feature-fix-swarm` ledger.
- `spec-status` now resolves the requested spec's active or archived planning
  state explicitly, rejects ambiguous spec/planning identities and mismatched
  run ledgers, and reports PID liveness only when it is attributable to the
  current worktree.

## v5.0.4 — Canonical symlinked backup sources (2026-08-01)

### Fixed

- Backup snapshots now support intentional symlinked configuration roots such
  as `~/.claude` by resolving the source parent once and copying exclusively
  through its pinned canonical directory descriptor.

## v5.0.3 — Canonical symlinked cache parents (2026-08-01)

### Fixed

- Private backup setup now supports an intentional symlink at `~/.cache` by
  resolving and opening its canonical directory once, while all
  feature-fix-swarm descendants remain no-follow and descriptor-anchored.

## v5.0.2 — Fail-closed review and installer races (2026-08-01)

### Fixed

- Project-scope skill, manifest, rollback, uninstall, and legacy-migration
  mutations now resolve through no-follow directory descriptors, reject
  post-preflight collisions, and preserve concurrently created destinations.
- User and project legacy migration, transactional rollback, and private backup
  creation/copy/restore now remain anchored to verified directory descriptors;
  symlink swaps fail closed without reading, chmodding, or writing their targets.
- GPT-5.6 evaluation selection now treats `CRITICAL` findings as severe and
  rejects non-boolean gate results in both the pairwise and complete-matrix
  selectors.
- Opposite-host adversarial review now receives half of the shared deadline,
  allowing substantive Claude reviews to finish while retaining a complete
  bounded fallback slice.
- Project-scope documentation now states explicitly that scope controls FFS
  discovery while the pinned full GSD profiles remain global and
  upstream-owned.

## v5.0.1 — Deterministic consumer reconciliation (2026-08-01)

### Fixed

- Project manifests now preserve `installed_at` on same-release reinstalls and
  record vendored sources relative to the project, so committed installs are
  portable and idempotent under CI verification.
- `--reconcile-consumer` now copies the complete drift-checked shell/Python
  runtime surface while preserving consumer files named in the local fork
  allowlist. Reconciliation now rejects symlinked destinations and performs
  no-follow, directory-relative atomic replacements.
- Installer cache and backup payloads now use private directory/file modes.
- Codex adversary reviews run without execution-capable tools or ambient API
  provider overrides, and isolated runs accept GSD skills only when their
  global installation matches the upstream manifest hashes.
- Nested `prompt-master` patch markers are classified as patch syntax rather
  than source whitespace by Git's diff hygiene checks.
- `model-fallback.sh` no longer uses the here-doc command-substitution quoting
  form that macOS Bash 3.2 accepted in syntax checks but rejected at runtime.

## v5.0.0 — Codex/GPT-5.6 modernization (2026-08-01)

### Added

- Native project and user installation scopes for Codex's canonical
  `.agents/skills` discovery root, plus `ffs.doctor/v1`, managed backups,
  rollback, legacy-hash migration, and shared primary/worktree locking.
- Host-neutral dispatch contracts for every FFS skill and the new
  `continue-compact` resume skill. Codex uses `$skill`; Claude Code uses
  `/skill`.
- Typed judgment/execution/volume model requests, GPT-5.6 role defaults, an
  18-case reproducible evaluation corpus, and lint that rejects unqualified
  raw model identifiers. All runtime adversary entry points use the same typed
  dispatcher; exact requests disable model ladders and cross-host fallback.
  CI also rejects skill/runtime paths that bypass the typed dispatcher or
  expand retired raw-model environment variables.
- A pinned `prompt-master` integration at commit
  `d15eabbe5d2122eedc060bae8a771381e9873d1b` with a minimal Codex GPT-5.6
  compatibility patch.

### Changed

- Exact-pinned GSD Core at `@opengsd/gsd-core@1.9.1`; setup delegates GSD's
  complete Claude and Codex profiles to the upstream installer.
- Rebuilt isolated Codex runs around manifest-complete runtime bundles,
  writable OAuth copies with compare-and-swap refresh, Codex CLI
  `>=0.137.0,<0.147.0`, custom-provider refusal, and persisted resume tuples.
- Runner and evaluation OAuth refreshes share one user-global file lock and
  race-safe CAS implementation; interrupted evaluations synchronize from a
  `finally` boundary or preserve a private recovery copy.
- Default isolation is `approval_policy=never` plus `workspace-write` and a
  binary network mode. Unsandboxed execution now requires a consumed,
  run-bound 72-hour `sandbox:danger-full-access` grant.

### Removed

- New installs no longer write `.codex/skills`; known unedited legacy files
  from v4.13.0 through v4.22.0 are migrated through the historical hash
  catalog, while edited or unknown files are preserved.

## v4.22.0 — Buildomator borrows: sync-drift check, session handoff, base-branch resolver (2026-07-31)

Three patterns adapted from `buildomator/buildomator` (MIT, the plugin cousin
of this repo's gsd lineage), each targeting pain this repo has measured.

### Added

- `scripts/gsd/sync-drift-check.sh` (+ `tests/bats/sync-drift-check.bats`):
  vendor-drift detector for the whole packaged-lever surface — generalizes
  FALLBACK-017's single-file check. Consumer copies classify IN-SYNC /
  MISSING (warn) / FORKED (allowlisted, reason printed) / DRIFT (exit 1) /
  STALE-ALLOWLIST. Allowlist = `<filename> <reason>` lines, making deliberate
  forks auditable instead of destroyable-by-blind-sync (the 2026-07-31
  six-file drift incident, hand-measured, motivates this). Validated live
  against a real consumer: correctly separated 2 known forks from true drift.
- `hooks/gsd-checkpoint.sh` + `hooks/gsd-handoff-resume.sh`
  (+ `tests/bats/gsd-checkpoint.bats`): microcompact-surviving session
  continuity. Claude Code's microcompact strips tool outputs WITHOUT firing
  PreCompact, so PreCompact-only checkpoints go arbitrarily stale; the writer
  refreshes `.planning/run-state/HANDOFF.json` (ts, branch, head, dirty count,
  STATE.md position) at most once per 60s via mtime throttle; the SessionStart
  surfacer prints it + a `/gsd-resume-work` pointer. Both ALWAYS exit 0, inert
  until wired in a consumer's settings.json. Kill-switch `GSD_CHECKPOINT=off`.
- `scripts/gsd/base-branch.sh` (+ `tests/bats/base-branch.bats`): ONE default-
  branch resolver (`GSD_BASE_BRANCH` env → origin/HEAD symref → local
  main/master → `main`; offline-only, no `git remote show` — network in a
  pre-run wall is a hang risk). `canary-gate.sh`, `qa-coverage-adversary.sh`,
  and `scope-drift-gate.sh` defaults now resolve through it (fail-soft to
  `main` on a crippled PATH) instead of hardcoding `origin/main`, which
  silently diffed against a nonexistent ref on master/trunk repos. Explicit
  `--diff-base`/`--base`/env overrides unchanged.

## v4.21.0 — Cost-discipline borrows: tested fallback, advisor budget, verify blocks (2026-07-31)

Three patterns adapted from `Neeeophytee/ai-cost-cutter-skills` (MIT), fitted
to FFS's OAuth-only constraint (nothing routes through API keys or gateways).

### Added

- `scripts/gsd/fallback-rehearsal.sh` (+ `tests/bats/fallback-rehearsal.bats`):
  "a backup you never ran is a hope" — operator-invoked bounded smoke through
  BOTH fallback rungs (claude-opus-5 via claude CLI, gpt-5.6-sol via codex CLI,
  both vendors' API keys stripped), recording `tested_on` + per-rung results to
  the probe cache (`rehearsal.json`). Exit 1 on any failed rung. Never wired
  into CI (no OAuth there). `model-fallback.sh` now WARNs at fallback-ENGAGE
  time when the rehearsal record is missing ("never run") or >30d stale —
  advisory, an unrehearsed fallback still beats no fallback.
- `gates.py delegation-audit`: advisor-call budget. Premium-tier spawns
  (model pin containing `opus`/`fable`) are counted as advisor consults —
  `ADVISOR-CALLS: n` always printed; optional `--advisor-cap N` adds
  `ADVISOR-WARN` when exceeded. Advisory: the always-exit-0 contract holds.
- `scripts/verify-skill-blocks.py` (+ `tests/test_verify_skill_blocks.py`, CI
  step): anti-rot gate for skill prose. Every ` ```bash verify` fence in
  `skills/*/SKILL.md` is extracted and EXECUTED in a fresh temp dir
  (`REPO_ROOT` exported, hermetic, 120s bound); nonzero fails the build and
  names the skill. Plain ` ```bash` fences stay prose. Seeded: autonomy-grant
  (TTL constants 72h/168h ↔ `lib/gates.py`) and feature-implement (finish-tail
  levers exist on disk).

## v4.20.0 — Anti-entanglement: run-end finalizer, drift gate, 72h grants (2026-07-30)

Prompted by a consumer-repo estate audit (2026-07-30): 95 branches (~30
orphaned `gsd/phase-*`), 38 worktrees (26 dirty) with zero run-end GC, and
run bookkeeping (`.planning/**`, `spike-results/**`) riding every PR diff —
plus two operator pain points: grants expiring mid-run on multi-day work,
and long runs drifting from the plan (tunnel vision) with no check cheaper
than a per-turn hook.

### Added

- `scripts/gsd/run-finalizer.sh` (+ `tests/bats/run-finalizer.bats`): run-end
  estate cleanup, called from the finish tail after `assert-merged.sh` passes.
  Removes the run's clean worktree (dirty → routed to `/adopt-wip`), deletes
  the landed branch local+remote (squash-safe: only under merged-`headRefOid`
  proof — never a blind force-delete), prunes `gsd/phase-*` ancestors of the
  merged head, clears `.planning/run-state/` (denylist protects the gates
  evidence ledger). Fail-soft, always exit 0. Kill-switch `FFS_RUN_FINALIZER=off`.
- `scripts/gsd/scope-drift-gate.sh` (+ `tests/bats/scope-drift-gate.bats`):
  phase-boundary (never per-turn) advisory drift check. Deterministic mode:
  diff-vs-`files_modified` classification (threshold `GSD_DRIFT_THRESHOLD_PCT`,
  default 20%) + `PHASE GOAL:` re-anchor line. `--judge`: ONE bounded
  cross-vendor verdict via adversary-host (`DRIFT-VERDICT: ON-TRACK|DRIFT`).
  Wired at three phase-frequency points: `gsd-run.sh` pre-phase wall,
  feature-implement Step 5 (with `--judge`), review-gate-command (stderr-only).
  Kill-switch `GSD_DRIFT_GATE=off`; test seam `GSD_DRIFT_JUDGE_CMD`.

### Changed

- `lib/gates.py`: `GRANT_DEFAULT_TTL_HOURS` 24 → **72** (cap unchanged at
  168h). 24h grants expired mid-run on multi-day agentic work; 72h covers the
  window while still expiring within the week. `skills/feature-spec/SKILL.md`
  **v2.4.0 → v2.5.0** and `skills/autonomy-grant/SKILL.md` **v1.1.0 → v1.2.0**
  updated to match (feature-spec's explicit `--ttl-hours 24` was overriding
  any default and is now 72).
- `skills/review-gate/SKILL.md` **v1.8.1 → v1.9.0**: reviewer-facing diff
  capture now excludes `.planning/` and `spike-results/` via git pathspecs —
  run bookkeeping is not production source and ballooned review diffs by
  hundreds of doc files on long spec branches.
- `skills/feature-implement/SKILL.md` **v2.11.0 → v2.12.0**: Step 5 gains the
  scope-drift gate (advisory, `--judge`); Step 6.5 gains the run-end finalizer
  after `assert-merged`.

## v4.19.0 — Fable-guide alignment + measurable runs (2026-07-30)

Prompted by a consumer-repo finding: gsd phases were taking 30–50 min and the
run logs carried no timestamps, so the question could only be answered by
counting the gate pipeline by hand. Two of the three time sinks were config
drift (the consumer's `.planning/config.json` had lost the fable planner pin
and was running 7 opus passes per phase against this package's own template
of 4); the third was unmeasurable. This release fixes the measurement gap and
folds in Anthropic's Fable 5 prompting-guide practices.

### Added

- `scripts/gsd/gsd-run.sh`: the on-disk run log now carries per-line
  `[YYYY-MM-DDTHH:MM:SS]` stamps (terminal stream unchanged), so phase
  durations are derivable from any run log after the fact.
- `docs/model-tiers.md` § "Fable-era operating notes": parallel dispatch,
  short-guards-over-checklists, effort-before-repin — the three guide
  practices this package relies on.
- `skills/feature-implement/SKILL.md` **v2.10.0 → v2.11.0**: drive-loop guard
  gains the guide's context-limit line ("do not stop … on account of context
  limits") and a Fable dispatch-discipline block (fan out independent work,
  never block per subagent return; plan gauntlet stays serial).

### Changed

- `templates/gsd-config.base.json`: `dynamic_routing.max_escalations` 2 → 1.
  Two escalations meant a soft-failing gate could run three times at rising
  tiers — a 3× flake tax; one escalation captures the recovery value.

## v4.18.0 — Reconcile the openclaw vendored fork; single source of truth (2026-07-29)

The `openclaw` monorepo vendors this package at `packages/feature-fix-swarm/`.
That copy had diverged in BOTH directions since v4.14.5: it was missing six
releases of upstream work, and it carried ~1,300 lines of hardening that was
never pushed back here. A one-way sync in either direction would have destroyed
real work, so every differing file was classified against the v4.14.5 baseline
and merged three-way. This release lands the openclaw side upstream so the two
trees are byte-identical again.

### Added — recovered from the openclaw fork

- **`scripts/gsd/model-fallback.sh` recovery hardening** (179 → 446 lines). The
  upstream version restored blind: it walked the config with no guard, so a
  legacy marker whose path had two valid readings (a literal dot-bearing key vs
  a nested path) could restore into the wrong node — silently and
  unrecoverably. The recovered version detects that ambiguity and skips with a
  reason, verifies the current value against what the lever actually wrote
  (CAS) before overwriting, prunes the marker to only unresolved paths so a
  restored entry is never re-applied on a later run, and skips the config write
  entirely when nothing changed (it used to truncate and rewrite a file it had
  no edits for). Test coverage went 15 → 31 cases (FALLBACK-016..030).
- **`lib/runtime_proof.py` + `lib/gates.py` hardening**, with their suites
  (`test_runtime_proof.py`, `test_gates.py`). Pytest 325 → 392.
- **bats coverage** for `adversary-host`, `int-review-gate`, `plan-adversary`.

### Fixed

- **`docs/promotion-protocol.md` genericization regression.** The openclaw copy
  had grown a vendor-specific section (a named deploy platform, tenant slugs,
  and cross-references to spec directories that do not exist in this repo). It
  failed this repo's own `test_doc_has_zero_vendor_names` guard — a red test
  sitting in the vendored tree. The genericized version is canonical here; the
  vendor-specific content belongs in the consumer repo's own docs.

### Notes

- Reconciliation was three-way against v4.14.5 (`0785e83`), not a pick-a-side
  merge. Where both trees changed a file, both sets of changes were kept.
  `tests/bats/model-fallback.bats` is the one exception to blind union: both
  trees had independently added FALLBACK-013/014/015, so a union produced
  duplicate test names that bats rejects outright — the openclaw file was taken
  wholesale after verifying it is a strict superset (all 15 upstream tests
  present, plus 16).
- Line count is NOT a direction signal, and was not used as one: several docs
  are *shorter* upstream because v4.17.0's truth pass deleted content that the
  fork still carries.

## v4.17.0 — Edge-probe + package-legitimacy gates, Codex ladder repin (2026-07-29)

Forward-ports the two techniques from the abandoned v3.22.0 "gsd-core Option-A
borrow" WIP that main never got and nothing since replaced. That WIP is archived
verbatim at `origin/archive/v3.22-option-a-wip`; the rest of it is deliberately
NOT ported — its plan-checker gate is superseded by `plan-adversary.sh`
(cross-vendor, producer≠reviewer), its honest-verifier/abstain pass already
landed in review-gate, and its `check-gsd` hard-require is moot now that
gsd-core is a pinned repo-local dependency reconciled by `install_gsd_surfaces`.

### Added

- **spec-decompose 2.5.0 — Step 2.5 spec-completeness gate (edge-probe).**
  `gsd-plan-checker` verifies a plan against the requirements that got *written
  down*; a data-shape edge the spec never surfaced is invisible to it and it
  will be confidently silent about the omission. Step 2.5 walks each `REQ-NN`
  through a closed 8-category taxonomy (boundary / adjacency / empty / encoding
  / ordering / precision / idempotency / concurrency) behind a relevance filter,
  forces each applicable edge to `resolved` / `dismissed` (reason required) /
  `unresolved`, and writes `edge-coverage.md`. Soft gate: an `unresolved`
  applicable edge WARNs before plan-phase rather than vanishing. Self-contained
  — no runtime dependency.
- **feature-implement 2.10.0 — package-legitimacy pre-install gate.** Guards
  slopsquatting: LLM-hallucinated package names that squatters pre-register.
  Registry existence proves *registration*, not legitimacy, so an
  agent-discovered package stays `[ASSUMED]` until cleared. Absent from the
  registry → BLOCK (never silently substitute a similar name). Optional
  `slopcheck` verdict: `SLOP` → hard block even under `--autonomous`; `SUS` or
  `[ASSUMED]` → operator checkpoint through the existing grant ledger as
  `install:<pkg>`. Degrades gracefully when `slopcheck` is not installed.

### Fixed

- **Docs purge of flags and machinery that do not exist.** Every documented flag
  was grepped against the skill it belongs to; the phantoms are gone.
  `/feature-implement` was documented with `--qa-loop` / `--one` / `--resume` /
  `--qa-openclaw` / `--qa-telegram` (zero of which exist) — README's flag table
  said so directly under a heading claiming it was "verified against the shipped
  SKILL.md files". `/fix`'s four documented flags were all phantom. `/feature-spec`
  was missing four real ones. `RALPH_EXECUTOR` had no consumer anywhere;
  `RALPH_MAX_RETRIES` is a `--max-retries` flag on `scripts/ralph-retry.sh`, not
  an env var.
- **Ruflo removed from the docs** (`pipeline.md`, `gbrain-optional.md`) — it was
  retired in v4.0.0 but still described as the live orchestrator, including two
  `--ruflo` / `--no-ruflo` flags listed as "available". `gbrain-optional.md`
  compared gbrain to "ruflo agentdb + results.md"; the real fallback is
  `.feature-fix-swarm/learnings-archive.jsonl`, and it is mutually exclusive with
  the gbrain write, not additive.
- **`qa-ralph-loop.md` rewritten** against what actually runs: gsd-core's gate
  ladder is what gates a phase; `scripts/qa-swarm.sh` is real but wired only into
  the narrow debounced auto-QA hook, not into a `/feature-implement` flag.
- **Dropped doc sections describing absent scripts** — `scripts/harness-eval.sh`
  and `scripts/hooks/post-spec-write.sh` are referenced throughout `pipeline.md`
  but exist nowhere in the repo. Deleted rather than given an invented successor.

### Changed

- **Codex model ladder repinned to the 5.6 family** in `lib/dispatch.py` and
  `prompts/decompose-spec.md`: `gpt-5.6-luna` / `gpt-5.6-terra` / `gpt-5.6-sol`,
  matching `scripts/gsd/model-equivalents.sh` (the source of truth) which had
  already moved. The old `gpt-5.4-mini` / `gpt-5.4` / `gpt-5.5` names are
  retained in the tier sets and cost table so a `tasks.md` written before the
  repin still resolves to its intended tier instead of silently falling through
  to mid-tier.

## v4.16.0 — `/spec-status` skill (2026-07-29)

- **New skill `/spec-status [NNN]`** — answers "where are we?" for a spec run from
  evidence rather than recall. Fans out over git, `.planning/`, the gates ledger,
  runner state, evidence store, and hygiene checks; synthesizes a status report and
  a `/handoff`. Read-only apart from the report + handoff files it writes.
  `--continue-compact` chains compact prep; `--no-handoff` skips the handoff.
  Pins `GSD_RUN_ID`/`GATES_STORE` before any ledger read so a worktree-cwd decoy
  store cannot answer for the main one.
- **`setup.sh` installs skill `scripts/` directories.** The install loop copied only
  `SKILL.md`, so any skill shipping a helper script (the first is `spec-status`, which
  calls `scripts/collect-status-facts.sh` relative to its own dir) installed in a state
  where it could not run. Now the loop copies a `scripts/` dir when one exists.

## v4.15.0 — Docs overhaul, gsd-core 1.8.0, subscription-only auth guard (2026-07-24)

- **New docs (Diataxis):** `docs/getting-started.md` (tutorial), `docs/choosing-a-command.md`
  (how-to — `/office-hours` first, then `/feature-spec` / `/feature-implement` /
  `/fix` / `/task-swarm`, each with a "wrong choice when" clause), `docs/model-tiers.md`
  (explanation — fable/opus/sonnet/haiku routing, producer≠reviewer, fallback ladder),
  `docs/configuration.md` (reference — every knob, default, and `file:line` consumer).
  README rewritten around the same map; removed documented-but-nonexistent flags
  (`--qa-loop`, `--ruflo`, `--scope=`, `RALPH_MAX_RETRIES`, `RALPH_EXECUTOR`), added
  the real ones (`--autonomous`, `--adhoc`, `--no-finish`).
- **gsd-core bumped 1.6.1 → 1.8.0** (`setup.sh`).
- **Subscription-only auth guard closed a silent-billing gap:** every `claude`
  call site already stripped `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`, but no
  `codex` call site stripped `OPENAI_API_KEY` — and `model-fallback.sh`'s codex
  probe was stripping the *Anthropic* keys instead. Both vendor CLIs prefer an
  ambient API key over the logged-in OAuth session, so a stray key silently bills
  metered API instead of the subscription, with no error and no log line. Fixed
  in `model-fallback.sh`, `gsd-run.sh`, and `lib/run_state/audit.py`
  (`_subscription_env()` scrub before every `codex exec`/`subprocess.run` spawn).
  New `tests/bats/subscription-auth-guard.bats` (9 cases) locks in the strip at
  every call site plus blanket prohibitions on OpenRouter, metered SDK imports,
  direct model-endpoint HTTP calls, and `*_BASE_URL` overrides.
- Baseline held: pytest 325 passed, bats 296/296 (+9 new guard cases).

## v4.14.5 — Opus 5 model-pin bump (2026-07-24)

- **Opus tier repinned to `claude-opus-5`** (Opus 5 release). Every live pin
  moved off the retired `claude-opus-4-8`: `model-fallback.sh` fable→opus
  substitute, `security-model-fence.sh` security-touching planning fence,
  `gsd-run.sh` lead-model resolution, `adversary-host.sh` cross-model ladder.
  Comments in `model-equivalents.sh` and `harness-audit.py` follow.
- **No behavior change** beyond the resolved model id — tier semantics,
  fallback ordering, and the fable→opus fence are untouched. Consumer configs
  pinning the `opus` short alias need no edit; configs pinning the full
  `claude-opus-4-8` id resolve to a retired model and should be updated.
- Baseline held: pytest 325 passed, bats 287/287.

## v4.14.4 — mechanical drive liveness and credential-output guard (2026-07-13)

- **Mechanical single-flight:** `gsd-run.sh` now owns an atomic pidfile,
  machine-aware heartbeat lease, and status record for the entire probe+drive
  lifetime. A second invocation refuses with rc 75 while the owner is live; a
  dead stale owner is reclaimed safely. Codex prompts include the exact pidfile
  fallback.
- **No transcript credential dumps:** a Claude/Codex PreToolUse guard blocks
  Doppler value reads/downloads and Railway variable/config JSON output while
  preserving `doppler run` injection and `--only-names` inventory.
- **Consumer parity:** setup/reconciliation installs, registers, and byte-checks
  both runtime guards on Claude and Codex hosts.
- **Skill version:** feature-implement 2.9.4.

## v4.14.3 — exact GSD requirement ownership gate (2026-07-13)

- **No premature REQUIREMENTS completion:** before `/gsd-execute-phase`, FFS
  now rejects any phase whose PLAN frontmatter repeats a ROADMAP requirement or
  does not exactly cover that phase's requirement set. The headless runner
  checks before model probing, so rejection launches no stateful drive.
- **Preparatory plans stay honest:** `requirements: []` is explicitly supported.
  Each ID belongs only on the last plan that genuinely completes it; earlier
  invariant checks remain in `must_haves` without claiming completion.
- **Consumer parity:** setup and targeted reconciliation install and byte-check
  `requirement-ownership-gate.sh` alongside the host-native runner.
- **Skill versions:** feature-implement 2.9.3 and spec-decompose 2.4.2.

## v4.14.2 — Codex yielded-session single-flight contract (2026-07-13)

- **No duplicate long-running gates:** Codex GSD drives now receive an explicit
  two-level exec contract: a yielded orchestration cell is not command failure,
  and a returned `session_id` without `exit_code` must be polled with
  `write_stdin` until completion.
- **Single-flight by instruction:** autonomous executors are forbidden from
  launching a diagnostic replacement while the original test/build/deploy
  session remains alive. This closes the observed path where one real PG18
  gate became several concurrent disposable restores.
- **Skill version:** feature-implement 2.9.2.

## v4.14.1 — fail-closed cross-host fallback hardening (2026-07-13)

- **No stateful replay:** `gsd-run.sh` now probes the invoking CLI/model/quota
  with a fixed read-only prompt before launch. It can select the other vendor
  only at that boundary; any nonzero exit or timeout after the GSD drive starts
  returns bounded, same-host resume guidance and is never classified from task
  output or replayed across vendors.
- **Fail-closed review fallback:** ship review, plan review, QA coverage review,
  FULL-tier review, and honest verification try the opposite host first and
  allow one active-host fallback inside the original overall deadline because
  those calls are read-only. Degradation is explicit. Mandatory review blocks
  when both hosts fail, and GSD ship review requires exactly one line-anchored
  PASS/BLOCK verdict. Review prompts stream on stdin to avoid `ARG_MAX`; the
  optional `FFS_CROSS_VENDOR_FALLBACK=off` forensic switch disables both
  pre-launch host selection and the read-only retry.
- **True hard wall:** `run_bounded` arms TERM plus KILL on coreutils hosts and
  retains the Python process-group KILL rung, so TERM-ignoring descendants
  cannot turn a timeout into an indefinite wait.
- **Consumer reconciliation:** `setup.sh` verifies critical runtime bytes after
  installation, registers `cli-hang-guard.sh` for Claude and Codex, and adds
  `--reconcile-consumer <repo>` for a dependency-free targeted refresh.
- **Hang-guard parser:** token-aware, non-evaluating shell analysis replaces
  substring allowlisting. Path text, environment values, nested `sh -c`, and
  `$CODEX_BIN`/`$CLAUDE_BIN` forms no longer bypass the wall.
- **Skill versions:** feature-implement 2.9.1, review-gate 1.8.1,
  task-swarm 3.2.2, plan-decompose 1.5.1, fix 4.1.1, and spec-decompose
  2.4.1.

## v4.14.0 — bounded cross-vendor availability fallback (2026-07-13)

- **Native host remains first:** GSD starts on Codex/Terra when invoked from
  Codex and Claude/Sonnet when invoked from Claude.
- **No quota-reset stalls:** before a mutating GSD drive, a bounded read-only
  model probe checks the native host and can select the equivalent tier on the
  other vendor when the native CLI/model is unavailable.
- **Mutation safety:** vendor selection happens before execution; post-launch
  failures and timeouts are not replayed across vendors.
- **Review continuity:** review paths prefer the opposite vendor and can use a
  bounded active-host fallback when the preferred reviewer is unavailable.
- **Codex config-root selection:** unrelated project-local Codex agents no
  longer shadow the global GSD installation.
- **Large-diff-safe reviews:** adversarial prompts stream through stdin rather
  than one argv entry, avoiding host `ARG_MAX` limits.
- **Forensic control:** `FFS_CROSS_VENDOR_FALLBACK=off` disables cross-vendor
  selection when isolating one provider.

## v4.13.1 — self-healing lightweight task pipeline (2026-07-13)

- **Task Swarm now completes its own planning loop:** `/task-swarm` remains a
  DRY sequencer while `/plan-decompose` owns plan creation, task decomposition,
  opposite-host review, scoring, and bounded automatic repair.
- **No first-gate deadlock:** rejected plans and low-scoring decompositions are
  repaired and rechecked up to two times before the pipeline can stop.
- **Actionable terminal blocks:** exhausted repair budgets produce a findings
  artifact with score history and an exact resume command instead of the vague
  “mandatory plan gate” message.
- **Feature-pipeline parity without ceremony:** the lightweight path still uses
  the shared preflight, MAX-AUTH grant ledger, autonomous implementation,
  review, ship, and canary stages from the full feature pipeline.

## v4.13.0 — host-native GSD and symmetric cross-host review (2026-07-13)

- **Host-native GSD runner:** `scripts/gsd/gsd-run.sh` now detects the invoking
  harness and keeps ordinary execution on that vendor: Claude/Sonnet on Claude,
  Codex/Terra on Codex. Codex command spelling is translated from `/gsd-*` to
  `$gsd-*`; a missing native CLI fails instead of silently crossing vendors.
- **Executable model materialization:** generated Codex agent TOMLs translate
  Fable/Opus → `gpt-5.6-sol`, Sonnet → `gpt-5.6-terra`, and Haiku →
  `gpt-5.6-luna` by sourcing the existing `model-equivalents.sh` table.
- **Dual GSD surfaces:** setup reconciles both installed host runtimes even
  when the pinned GSD package was already present, closing the prior
  Claude-only install path.
- **Symmetric review:** both direct `/review-gate` and GSD's ship review use
  the opposite host with explicit models (Claude host → Codex Sol/xhigh;
  Codex host → Claude Opus).
- **Frontend routing audit:** `/fix` now spells GSD debug commands for both
  hosts, while `/task-swarm` and `/plan-decompose` keep ordinary resume,
  planning, execution, and learning commands host-native. Plan/task scoring
  gates now use the shared opposite-host adapter instead of hard-coded Codex.
- **Host detection hardening:** `FFS_HOST` is an explicit override,
  `CODEX_THREAD_ID` is recognized, ancestor executables are a fallback, and
  config roots such as `CODEX_HOME` no longer masquerade as session markers.
- **TDD coverage:** new Bats matrices cover native GSD invocation, host
  detection, model materialization, dual install wiring, and ship-review
  inversion.

## v4.12.0 — promotion evidence and fail-closed production-action gates (2026-07-12)

- **`lib/gates.py` promotion ledger:** adds validated, TTL-bounded `promote`
  records that bind an immutable artifact identity and staging evidence to an
  environment transition. Promotion evidence must come from `run-gate` and be
  bound to the same immutable artifact; caller-recorded exit codes, mutable
  tags, empty evidence, and malformed or expired records fail closed.
- **Production grant precondition:** `deploy:prod-*`, `flip:prod-*`, and
  `migrate:prod-*` actions now require both their normal operator grant and a
  fresh artifact-matching promotion record. Refusals record typed reasons for
  missing evidence, artifact mismatch, expiry, and missing staging
  counterparts; non-production grant behavior remains unchanged.
- **Auditable hotfix escape:** `hotfix:prod-*` remains operator-granted,
  requires a non-empty reason, emits a loud emergency banner, and writes a
  durable bypass record instead of relying on an environment kill switch.
  - **Preflight staging proof:** `kind: staging-proof` requirements read the
    promotion ledger and refuse empty, stale, or non-matching evidence.
  - **Parity-manifest wiring:** `check-grant --manifest` accepts both the legacy
    JSON map and the committed `surfaces:` YAML row shape without adding a
    runtime dependency, so `staging_instance: none` reaches the fail-closed
    `NO-STAGING-COUNTERPART` decision instead of failing at parse time.
- **Protocol documentation:** adds the vendor-neutral 12-rule promotion
  protocol and links it from `review-gate`, `preflight`, and
  `feature-implement`.
- **Acceptance coverage:** adds RED→GREEN unit and public acceptance tests for
  promotion recording, production refusal/approval, typed failures, hotfix
  auditing, staging-proof preflight, legacy-ledger compatibility, and protocol
  shape.

## v4.11.0 — re-port the dropped OpenWiki finish-tail ship-stage (2026-07-12)

The v3.21.0 conditional OpenWiki ship-stage (refresh + `git add openwiki/`
before the ship commit, warn+continue, silent no-op in repos without
`openwiki/`) was silently dropped in the v4.0 finish-tail rewrite and stayed
missing through v4.5 — every consumer ship since was wiki-blind unless the
spec author touched the wiki by hand (openclaw handoff
`2026-07-12-ffs-openwiki-ship-tail-handoff.md`).

### Added
- `skills/feature-implement/SKILL.md` (v2.7.0): finish-tail item 3 — OpenWiki
  ship-stage between the QA gates and `/review-gate` → ship, restoring the
  v3.21.0 semantics verbatim: diff-to-page mapping is LLM work, the marked
  bash block only stages; **best-effort by design** (FR-013/EDGE-007/008 —
  the wiki stage never blocks PR creation). Same
  `<!-- openwiki-wiring:ship-stage:begin/end -->` marker convention.
- `tests/bats/openwiki-ship-stage.bats` (OWS-001..003): drop-guard — markers
  present, block is warn+continue and stages `openwiki/`, stage ordered
  before ship. A future finish-tail rewrite that drops the stage now fails CI
  instead of drifting silently.

## v4.10.0 — dead-CLI hang guards: run-bounded lib + cli-hang-guard hook (2026-07-12)

A spec-298 "30-minute adversarial review" was a dead-process wait, not review
time: a `codex` subprocess hung and the orchestrator blocked on it forever.
Two independent defects fixed (forensics: the 2026-07-12 dead-codex handoff).

### Added

- **`scripts/gsd/run-bounded.sh`** — sourceable wall-clock bound primitive:
  `run_bounded <secs> <cmd...>` resolves `timeout` → `gtimeout` → a portable
  `python3 subprocess.run(timeout=)` rung (exits 124 on expiry, kills the
  child) → REFUSES with rc 124 without running the command. External CLIs are
  never executed unbounded, on any host shape. rc 124 feeds the callers'
  existing timeout/fail-open paths, so a refused call degrades to a logged
  skip instead of a silent forever-block. Installed into consumer repos by
  setup.sh alongside the levers that source it.
- **`scripts/hooks/cli-hang-guard.sh`** — PreToolUse (Bash) enforcement for
  the defect that actually bit: the ORCHESTRATOR invoking codex ad-hoc from
  its Bash tool, bypassing every lever guard. Blocks (exit 2) hang-prone
  execution forms (`codex exec|review`, `claude -p/--print`) that carry no
  visible bound and aren't a sanctioned lever invocation; version/status
  probes pass. Fail-open on unparseable input; kill-switch
  `CLI_HANG_GUARD=off`. Sibling of delegation-enforcer.sh; consumer repos
  register it on PreToolUse `Bash`.
- **Bats:** `run-bounded.bats` (per-rung ladder incl. refusal-without-running
  + adversary/ship-gate integration), `cli-hang-guard.bats` (block/pass/
  kill-switch/fail-open matrix), `model-fallback.bats` FALLBACK-012 (real-CLI
  probe branch is bounded).

### Changed

- **`adversary-host.sh::adversary_invoke`** — the deliberate "run unwrapped
  rather than hard-fail" branch (hit when neither `timeout` nor `gtimeout`
  exists, i.e. every stock macOS/BSD host) is GONE; all invocations route
  through `run_bounded`. The plan-adversary.bats test asserting unwrapped
  execution now asserts the bounded python3 rung.
- **`review-gate-command.sh`** — same: no-timeout branch removed; bound is
  `GSD_REVIEW_TIMEOUT` (default 600s). A hung codex now yields the fail-soft
  `APPROVED (fail-soft)` verdict instead of stalling the SHIP gate forever.
- **`model-fallback.sh`** — the worst site (no timeout on ANY branch, runs as
  a wall at the top of `/feature-implement`): both real-CLI probes now run
  under `run_bounded "${GSD_MODEL_PROBE_TIMEOUT:-120}"` with `</dev/null`;
  a reaped probe reads as "unavailable" (the lever's existing fail-soft
  direction).

### Open decision (surfaced, not silently chosen)

On a coreutils-less host the refusal rung means the adversary is SKIPPED
(logged, fail-open) — consistent with every caller's existing degrade
philosophy. But the plan-adversary bounce is MANDATORY on high-blast plans
(auth/RLS/payments): if skip-and-warn is the wrong default there, gate on
rc 124 at the caller (plan-adversary.sh) and hard-stop with "install
coreutils". run-bounded.sh stays a pure bounding primitive either way.

## v4.9.0 — review-gate v1.6.0: concurrent defect passes + reviewer context contract (2026-07-11)

- **Concurrent passes:** Pass 1/2/3 + FULL-tier extra adversary are independent
  (same `$DIFF`, no cross-reads) and now dispatch in parallel — Agent calls in
  one message + `run_in_background` bash. STANDARD wall-clock ~13–20 min serial
  → ~8 min (slowest-pass = 480s adversary timeout). Merge-and-rank and
  refute-or-promote stay sequential (consume the merged finding set).
- **Reviewer context contract (evidence-backed):** reviewers run FRESH — fed the
  artifact (diff + read-only repo) and the fixed pass prompt, never the author's
  reasoning trail. Cross-context review F1 28.6% vs 23.8% context-aware vs 24.6%
  same-session self-review (arxiv 2603.12123): showing the reviewer the
  production context makes it statistically indistinguishable from self-review.
  Forbidden both ways: leaking author rationale into dispatch prompts, AND
  starving the reviewer of the artifact — fresh ≠ less artifact.
- **plan-decompose v1.2.0 — guarded codex invocation (hang prevention):** Steps
  3 + 6 now mandate `timeout 540 codex exec … </dev/null >"$OUT_FILE"` with a
  bare exit code (never `| tail`). Root cause of an observed 30+ min "review"
  of a ~120-line plan: bare `codex exec` with stdin open blocks forever on
  "Reading additional input from stdin…" — the run never started reasoning
  (2-line session file, no agent message); the guarded retry finished in <9 min.
  `RC=124` is fail-soft advisory-skip (downstream opus plan-checker still
  gates). Prompt gains a scope line (no repo-tree/skill reads — plan text only),
  killing the other big review-time multiplier: repo-wandering.

## v4.8.0 — refactor: /fix and /task-swarm become thin front-ends over /feature-implement (2026-07-11)

Operator ask: one home for the delegation machinery. `/fix` had drifted into a
parallel pipeline — its own inline model-fallback wall, its own gsd-run
invocation, its own verify/review-gate tail, no grant ledger, no delegation
histogram. `/task-swarm` inlined the preflight + grant bash that `/preflight`
and `/autonomy-grant` already own. This release makes `/feature-implement` the
single execution engine; the two front-ends carry only what is unique to them.

- **feature-implement v2.6.0 — `--adhoc "<task>"` mode.** No spec/plan needed:
  same walls (check-preflight for `--autonomous`, model-fallback, security
  fence, `GSD_RUN_ID` export — ledger key `adhoc-<slug>`), execution via
  `/gsd-quick` (TDD RED/GREEN commit trail), completion authority =
  `workflow.test_command` gate, and the SAME finish tail (browser gate →
  qa-coverage adversary → review-gate → grant-walled ship → merge backstop →
  learnings harvest) + delegation-histogram report as spec runs.
- **fix v4.0.0 — thin front-end.** Keeps its unique value: root-cause
  investigation with the `/gsd-debug` routing criteria, and the fix-specific
  `/qa-only` loop (max 2). Everything else routes to
  `/feature-implement --adhoc`. Removed: inline model-fallback block, inline
  gsd-run/gsd-quick invocation, inline review-gate step, `--no-audit` +
  `--no-review-gate` flags (use `--no-finish` passthrough).
- **task-swarm v3.0.0 — pure sequencing.** Steps 2–3 now invoke the
  `preflight` and `autonomy-grant` skills instead of restating their bash
  (gates.py resolver block deleted; the invoked skills carry the commands).
  Framed as the with-planning sibling of `/fix`: plan-decompose → preflight →
  grant → `/feature-implement NNN --autonomous`.
- No `lib/gates.py` or `scripts/gsd/*` changes — run ids were already
  free-form (`adhoc-<slug>` keys the same grant/pending/preflight ledger), and
  `review-gate-command.sh` already reads exported `GSD_RUN_ID`.

## v4.7.0 — feat: MAX-AUTH auto-grant default — authorization moves to launch (2026-07-11)

Operator pain: the grant screen fired at the END of `/feature-spec` (hours
after launch, after specify→plan→clarify→decompose), stalling the pipeline
until the operator returned. The *enumeration* of gates genuinely needs the
finished plan — but the *approval* never did. This release moves the approval
moment to minute 1 and makes maximum authorization the default.

- **feature-spec v2.4.0 — MAX-AUTH default + `--gated` opt-out.** Launching
  without `--gated` IS the approval: a launch notice announces it, and Step 6
  then auto-records every gate enumerated from plan.md/tasks.md — no blocking
  screen. The granted list + one-line rollback per action lands in the
  completion summary for review. `--gated` restores the pre-v2.4.0
  present-and-wait screen; `--no-grant` still skips the ledger entirely.
- **task-swarm v2.3.0 — zero planned stops.** Step 3 auto-grants by default;
  `--gated` passes through to the review screen. Rules/diagram updated from
  "one operator stop" to "zero planned stops".
- **autonomy-grant v1.1.0 — MAX-AUTH mode documented.** Distinct from the
  banned `push:*` wildcard: the ledger still holds exact typed entries walked
  from the plan, TTL'd and run-bound, and an action NOT enumerated still
  stops + records `pending`. The safety floor is unchanged in both modes —
  max-auth widens what is foreseen-and-approved, never what is allowed
  unforeseen.
- **feature stub** — wording updated to match.

No `lib/gates.py` changes: grant/check-grant/pending mechanics are identical;
only the moment the operator says yes moved.

## v4.6.0 — feat: provenance review contract + merge terminal-state backstop + pipeline-skill wiring (spec 005, 2026-07-11)

- **review-gate v1.5.0 — provenance finding contract** (adapted from
  steipete/agent-scripts `github-deep-review`): pass 1+2 findings now carry
  `CAUSE` (root cause, not symptom), `PROVENANCE` (introduced-by-diff vs
  pre-existing, confidence `clear|likely|unknown`), and `PROOF` (how to verify
  the fix). Verify-the-reviewer checks `unknown`-confidence claims FIRST —
  they are the likeliest to be stale/wrong.
- **`scripts/gsd/assert-merged.sh` (new lever)** — terminal-state assertion
  after any PR merge (pattern: agent-scripts `landpr` "state==MERGED, never
  CLOSED"): exit 0 MERGED / 1 CLOSED-without-merge (loud) / 2 still OPEN /
  3 gh error. feature-implement's finish tail (v2.5.0) requires exit 0 before
  a `merge:pr` grant is recorded consumed.
- **feature-implement v2.5.0 — finish-tail merge step**: fail-soft ladder —
  `/land-and-deploy` when available in the session (merge → CI/deploy wait →
  prod verify), else bare `gh pr merge`; either path backstopped by
  assert-merged.sh. Optional `/landing-report` queue snapshot after ship.
- **feature-spec v2.3.0 — Step 2.5 plan-review gauntlet (fail-soft)**: if an
  `/autoplan` skill is available, run it on the fresh plan (CEO → design →
  eng → DX auto-review) between speckit.plan and clarify; absent → silent skip.
- **setup.sh installer-completeness fix**: `gsd/model-fallback.sh` and
  `gsd/security-model-fence.sh` were invoked by feature-implement Step 2 but
  never in the install manifest — consumer repos silently no-op'd both. Added,
  plus `gsd/assert-merged.sh`; all three static-asserted in
  `tests/bats/setup-install.bats`.

## v4.5.1 — feat: cross-vendor model equivalence + 3-leg fable fallback with recovery (spec 004, 2026-07-10)

- **`scripts/gsd/model-equivalents.sh` (new)** — sourceable Claude<->Codex
  model equivalence lib (`codex_equiv_model`/`codex_equiv_effort`/
  `claude_equiv_model`), the executable home for the mapping table in
  `docs/fable-pilotfish-alignment.md`; fail-soft on unknown input (echoes
  input, returns 1). Also documents that codex CLI 0.144's `model_reasoning_effort`
  enum is `none|minimal|low|medium|high|xhigh` — `ultra`/`max` are CLI-accepted
  aliases, not enum members; canonical top is `xhigh`.
- **`scripts/gsd/model-fallback.sh` (3-leg + recovery)** — the fable->opus
  fallback is no longer sticky: a `gpt-5.6-sol` cross-vendor probe leg records
  whether compensation is live (marker `.planning/fable-fallback.json`,
  `mode: codex-sol|opus-only`), and once fable is available again the lever
  restores ONLY the JSON paths it rewrote — never a blanket opus->fable
  substitution, which would incorrectly flip intentional opus pins.
- **`scripts/gsd/plan-adversary.sh`** — while a `mode: codex-sol` fallback
  marker is present, the low-blast skip is disabled (every plan gets the
  xhigh sol pass, since that pass is standing in for the missing fable
  planning-tier review).

## v4.5.0 — feat: orchestration-hardening levers — delegation auto-pin, findings-queue dedup, composite liveness, harness scorer (spec 003, 2026-07-10)

Eight enhancements from spec 003 (`specs/003-orchestration-hardening/`): six new
levers plus their skill wiring. `setup.sh` installer manifests are extended to
ship all six to consumer repos (INT-003), static-asserted by
`tests/bats/setup-install.bats`.

- **`scripts/hooks/delegation-enforcer.sh` (new, fail-open)** — PreToolUse
  hook that auto-pins unpinned Agent/Task spawns to the model resolved from
  `.planning/config.json` `model_overrides[subagent_type]`, so unpinned
  sub-agent spawns stop silently drifting to a premium default tier.
- **`scripts/gsd/security-surface.sh` + `scripts/gsd/review-tier.sh` (new)** —
  shared security-surface keyword extraction, and a diff risk-tier detector
  (light/standard/full) so review-gate sizes its pipeline to actual risk
  instead of paying a 40-file auth review for a 2-file docs change.
- **`scripts/gsd/liveness-check.sh` (new)** — AND-of-death 3-signal composite
  liveness probe (pid/mtime/ship-grant); an autonomous run is only declared
  DEAD when all three signals agree, so one transient failed probe never
  kills an overnight wave.
- **`scripts/gsd/learnings-harvest.sh` (new, fail-soft)** — gbrain-or-archive
  learnings harvester: writes finished-run `learnings*.jsonl` entries to
  gbrain when healthy, else atomically appends to
  `.feature-fix-swarm/learnings-archive.jsonl`; always exits 0.
- **`scripts/harness-audit.py` (new, advisory)** — stdlib-only 0-100 scorer
  for the installed `~/.claude` harness itself (dangling skill symlinks,
  vendored-copy drift, dead model pins, unregistered hooks), wired into
  `preflight`.
- **`findings-queue` in `lib/gates.py` (new)** — `add|list|resolve` with
  full-sha256 structured signatures over `[file, normalized_issue]`, atomic
  dedup under the existing store lock; wired into `review-gate`'s tier
  gating.
- **`code-uplift` gains `--slop-only <diff-base>`** — deslop fast path with a
  green-baseline refusal wall, and `--autonomous` now mandates the
  feature-implement preflight/grant walls.
- **Consult-lever skill wiring** — `adopt-wip` calls `liveness-check.sh`,
  `preflight` calls `harness-audit.py`, `fix` routes through `/gsd-debug`;
  all RED-first via `tests/bats/int-consult-levers.bats`.

## v4.4.0 — feat: browser-QA gate, testing doctrine, code-uplift skill, host-aware adversaries, spec-time prior-art search (2026-07-10)

Grounded on 2026 testing research (mock-drift/jsdom as the "green tests, broken
browser" root causes), the Canary CLI's machine-readable `results.json`, and the
GPT-5.6 model family release (`gpt-5.6-sol`/`-terra`/`-luna`, effort tiers now
`none…xhigh|max` on all three).

- **`scripts/gsd/canary-gate.sh` (new, fail-closed)** — browser-QA gate: web-touch
  diffs (pattern shared with `browser-proof.sh`) require fresh headless Canary
  results with `status=="passed"`, `consoleErrors==0`, `networkFailures==0`;
  staleness-checked against HEAD. Wired into the `feature-implement` finish tail
  (before `/review-gate`) and seeded as a literal ROADMAP gate command for
  UI-touchable stories by `spec-decompose`. 18 bats cases.
- **`skills/testing-policy/` (new, v1.0.0)** — single home for FFS testing
  doctrine: mock-minimization ladder (boundary-only, never first-party module
  mocks, don't-mock-what-you-don't-own), real-browser over jsdom for UI truth,
  console/network tripwires, independent test authorship, BDD-as-Canary-step-input,
  80% coverage floor, ≤60s smoke design. Other skills reference it (DRY).
- **`skills/code-uplift/` (new, v1.0.0)** — findings-driven sibling of
  feature-spec/feature-implement for code that already exists: cross-model review
  sweep (opus + opposite-CLI adversary + test auditor + dead-code scout) →
  REVIEW.md → gsd seed (fix-critical → refactor → test-uplift → e2e-smoke phases)
  → same execute loop, coverage-floor + canary gates, review-gate finish tail.
- **`scripts/gsd/adversary-host.sh` (new lib) + host-aware `plan-adversary.sh`** —
  cross-model adversaries now detect the orchestrating CLI (review-gate's
  convention: `CODEX_SESSION_ID`/`CODEX_HOME`/`CODEX_AGENT`) and pick the OPPOSITE
  vendor: claude host → `codex exec` `gpt-5.6-sol` xhigh; codex host →
  `claude -p --model opus` (API-key env scrubbed → OAuth). Codex-orchestrated FFS
  runs now get genuinely cross-vendor plan review. 8 new bats cases.
- **`scripts/gsd/qa-coverage-adversary.sh` (new, advisory)** — dual-CLI QA: after
  the browser gate, the opposite-vendor model (default `gpt-5.6-terra` @ `high` —
  gap-finder tier, not judge tier) reads the Canary step list + diff and emits
  line-anchored `MISSED: <flow>` findings; wired into the finish tail as a
  triage-before-ship step. 5+ bats cases.
- **`feature-spec` v2.2.0 — spec-time prior-art search (fail-soft)** — parallel
  haiku skill-scout (`/find-skills` + compiler CLI) + sonnet OSS researcher
  (`gh search repos/code`, `PRIOR_ART_MIN_STARS` default 200, README+source
  applicability verification) → `specs/NNN/prior-art.md`; opus adopt/port/wrap/
  build adjudication only when a vindicated candidate exists; plan.md must cite
  the decision.
- **Effort-tier guidance** — GPT-5.6 family equivalences documented (sol↔opus/
  fable, terra↔sonnet, luna↔haiku); `max` effort reserved for escalated disputes
  on high-blast gates (`PLAN_ADVERSARY_EFFORT=max`).
- `feature-implement` 2.2.0→2.3.0, `spec-decompose` 2.2.0→2.3.0,
  `feature-spec` 2.1.0→2.2.0.
- **Review-gate hardening round (claude pass1/pass3 + codex `gpt-5.6-sol` xhigh
  adversarial):** every finding reproduced by a failing test before its fix —
  canary-gate fail-open holes closed (unresolvable diff-base, GNU-stat garbage
  mtime, incomplete/zero-total summaries, C-quoted filename evasion, `--diff-base`
  no-value abort), adversary sandbox pinned `read-only` against prompt injection,
  BSD/macOS `timeout` fallback, qa-coverage `--diff-base` hang, setup.sh installer
  manifests extended (both new skills + all 4 gsd scripts, static-asserted by
  `tests/bats/setup-install.bats`), code-uplift `--autonomous` now mandates the
  feature-implement preflight/grant walls, prior-art researcher gained an
  untrusted-content boundary. Recorded follow-up: canary evidence binds by
  freshness only — revision/base-URL/scenario provenance binding stays with
  `runtime_proof.py` (documented in canary-gate header).

## v4.3.0 — feat: Fable-aligned routing rebalance + plan-stage cross-model adversary (2026-07-10)

Grounded on the Anthropic "Prompting Claude Fable 5" guide, Ken Huang's Fable 5
field notes, and jnuyens/gsd-plugin's model catalog (heavy→opus / standard→sonnet
/ light→haiku with fable as a gated heavy-tier swap). Two changes:

- **`templates/gsd-config.base.json` routing rebalance** — `gsd-plan-checker`
  fable→**opus** (a checker on the same model as the planner is
  near-self-critique; Fable's own guide says fresh-context verifiers beat
  self-critique — model diversity is the point), `gsd-debugger` sonnet→**opus**
  (root-cause is the highest-value reasoning; "start at the top of your
  difficulty range"), `gsd-integration-checker`/`gsd-nyquist-auditor`
  opus→**sonnet** and `gsd-research-synthesizer`/`gsd-codebase-mapper`
  sonnet→**haiku** (mechanical audit/scout work; upstream gsd-plugin routes all
  four light/standard). Roughly cost-neutral; opus moves to where adversarial
  reasoning pays.
- **`scripts/gsd/plan-adversary.sh` (new) + `workflow.plan_bounce` wiring** —
  plan-stage cross-model adversarial review at gsd's native bounce seam
  (`plan_bounce_script`, invoked per PLAN.md before execute-phase). High-blast
  plans (auth/RLS/payments/migrations/…) get a pinned `gpt-5.6-sol` @ `xhigh`
  review; findings are APPENDED to the plan (bounce is restore-on-nonzero, so
  exit is always 0) and the opus plan-checker re-run adjudicates — GPT finds,
  opus judges. Low-blast plans skip (zero cost); fail-soft without the codex
  CLI; kill-switch `PLAN_ADVERSARY=off`; env overrides `PLAN_ADVERSARY_MODEL`
  / `_EFFORT` / `_KEYWORDS` / `_BIN` / `_TIMEOUT`. Tests:
  `tests/bats/plan-adversary.bats` (7/7). Skills: `spec-decompose` v2.2.0
  (documents the seam), `plan-decompose` v1.1.0 (pins its existing codex plan
  review to the same adversary tier).

## v4.2.0 — feat: orchestrator delegation discipline — trip-wire rule + histogram + delegation-audit lever (2026-07-09)

Retro finding from a real long run: the review ladder routed correctly
(opus/sonnet/haiku all landed on the right work) but the **orchestrator's own
cost discipline** drifted — build spawns left unpinned inherited a premium
host tier after a mid-run model switch, and the host drained several
mechanical loops (`sed -i` restamps, `git rebase` conflict drains,
doc-extraction loops) inline instead of delegating them.

- **`feature-spec` SKILL.md (v2.1.0)** — new "Orchestrator self-discipline
  (trip-wires)" paragraph in the Delegation discipline section: names the
  trip-wires that must be delegated, the legitimately-inline exceptions, and
  the mandatory `model` pin on every spawn.
- **`task-swarm` (v2.2.0) / `feature-implement` (v2.2.0)** — results.md /
  final-report lines now carry a `models={opus:N,sonnet:N,haiku:N,fable:N,
  inline-mechanical:N}` histogram so drift is visible in the run record, not
  just in retro.
- **`lib/gates.py delegation-audit <transcript.jsonl> [--threshold N]`
  (new, advisory, always exit 0)** — scans a Claude Code session transcript
  for main-loop Agent/Task spawns (histogram by model pin, flags unpinned
  build/rebase/prep spawns) and main-loop Bash trip-wires (rebase drains,
  restamp/doc-extraction loops), excluding sub-agent sidechains and
  legitimate CI-poll loops. `tests/bats/gates-delegation-audit.bats` (7/7).

## v4.1.0 — feat: Fable-5/pilotfish alignment — security model fence + anti-early-stop (2026-07-09)

Three deltas from auditing FFS against the Fable-5 prompting guide and the
pilotfish multi-model orchestration pattern (full rationale + the
already-implemented inventory: `docs/fable-pilotfish-alignment.md`):

- **`scripts/gsd/security-model-fence.sh` (new) + `tests/bats/security-model-fence.bats`.**
  Security-touching specs (auth/RLS/payments/crypto keywords in the seeded
  planning docs) force `gsd-planner`/`gsd-plan-checker` fable→opus even when
  fable is available — Fable's classifiers can false-refuse benign
  defensive-security work and stall an autonomous run silently. Handles both
  the `fable` alias and the resolved `claude-fable-5` ID; fail-soft, never
  silent; executor/verifier bindings untouched. Wired after `model-fallback.sh`
  in `/spec-decompose` Step 2 and `/feature-implement` Step 2.
- **Anti-early-stop reminder** ("check your last paragraph…") in the
  autonomous orchestrator loops of `/feature-implement` and `/task-swarm` —
  Fable's documented long-run early-stop mitigation. Orchestrator-level only;
  per-sub-agent coverage would need a gsd-core change.
- **`prompts/decompose-spec.md` marked LEGACY** (the `[model:]`/`[agent:]`
  grammar was retired by spec-decompose v2.0.0; live routing is gsd
  `.planning/config.json`) and the false "fable downgrades to sonnet on the
  Ruflo path" clause removed.

`spec-decompose` / `feature-implement` / `task-swarm` 2.0.0 → 2.1.0.

## v4.0.3 — fix: wire the fable→opus model-availability preflight into /fix (2026-07-06)

The `/fix` skill drove gsd-core's `/gsd-quick` loop headless but never ran the
model-availability preflight (`scripts/gsd/model-fallback.sh`) that
`/feature-implement` runs before spawning. So a dead premium pin (Fable dropped
off the OAuth subscription) would error the `gsd-planner`/`gsd-plan-checker`
spawn at loop start instead of being rewritten fable→opus. The lever and its
bats test already shipped (v4.0.2) — only the `/fix` call site was missing.

Fixed by adding the same fail-soft preflight block `/feature-implement` uses,
ahead of the `gsd-run.sh /gsd-quick` invocation. Best-effort, non-silent: it
warns on skip/failure rather than no-op'ing quietly. `skills/fix` 3.0.0 → 3.1.0.

## v4.0.2 — fix: gsd-config template used full model IDs, breaking Claude Agent spawns (2026-07-06)

`templates/gsd-config.base.json` shipped `model_overrides` (and
`dynamic_routing.tier_models`) as full Claude model IDs (`claude-sonnet-5`,
`claude-fable-5`, `claude-opus-4-8`) plus `resolve_model_ids: true`. On the
Claude runtime, gsd-core's `resolveModelInternal` returns a `model_overrides`
value **verbatim** (no alias mapping — the `CLAUDE_POLICY_ID_TO_ALIAS` safety
net exists only on the `model_policy` path, per gsd-core #1133/#1144), and the
Claude Agent tool's `model=` accepts only aliases (`sonnet`/`opus`/`haiku`/
`fable`). So every seeded gsd project spawned its subagents on the **parent
session's** model instead of the configured one (observed: 10 `gsd-executor`
agents running as the operator's session model, not `sonnet`). `resolve_model_ids:
true` independently forced full-ID output for any agent not in the override list.

Fixed by switching the template to bare aliases + `resolve_model_ids: false`
(gsd-core's own system default). Verified end-to-end: `resolve-model` now returns
`fable`/`sonnet`/`opus` for every override'd agent. Upstream gsd-core gap (docs
say full IDs are valid in `model_overrides`, but the Claude step-1 path never
maps them) filed separately — this template fix is alias-only and needs no
gsd-core change.

## v4.0.1 — port two Option-A findings from the gsd-core-eval spike (2026-07-06)

Reconciles `spike(001): gsd-core@1.6.1 adoption eval` (#25) against the shipped
v4.0.0 full-replacement design: two genuinely additive capabilities ported
onto their current file versions (both self-contained, no conflict with the
gsd-core loop); the rest of that spike's uncommitted edits were superseded by
what shipped or discarded as reintroducing ruflo.

### Added
- `skills/review-gate` (v1.3.0) — honest-verifier pass: spawns `gsd-verifier`
  for goal-backward phase verification with an abstain disposition
  (`insufficient_spec` → `human_needed`, never a false `passed`); composes
  with the existing defect-count gate (FAIL/ABSTAIN blocks even at 0
  CRITICAL/HIGH).
- `skills/feature-spec` — prior-work + vendor-doc grounding note: fail-soft
  `/openwiki-find` + `/cached-docs-find` lookups before Phase 1, grounding new
  specs in already-mapped subsystems and pinned vendor docs instead of
  guessing.

### Not ported (see `spike-results/gsd-core-eval/incorporation-plan.md` history)
- `lib/gates.py` `check-gsd` — designed for a "gsd optional, ruflo primary"
  world that no longer exists; the honest-verifier port above uses a minimal
  inline agent-presence check instead.
- `skills/feature-implement` package-legitimacy/slopcheck gate — real
  supply-chain-safety value, but its task-execution loop moved into
  gsd-core's external executor in v4.0.0; needs a new home (hook or gsd-side
  wiring), not a doc edit. Tracked as follow-up, not landed here.
- `skills/spec-decompose` diff, `setup.sh` ruflo-reinstall path — both
  conflict with the shipped gsd-native design; discarded.

## v4.0.0 — GSD replaces ruflo as the orchestration engine (spec 002, 2026-07-06)

**BREAKING.** The ruflo MCP swarm executor is removed from FFS. Orchestration now
runs through the gsd-core loop (`@opengsd/gsd-core@1.6.1`, exact-pinned repo-local
dep); `lib/gates.py` remains the sole completion authority.

### Added
- `scripts/gsd/gsd-run.sh` — trimmed-MCP, auth-scrubbed headless drive runner
- `scripts/gsd/gates-test-command.sh` — gsd `workflow.test_command` target (run-gate + strict verify-done)
- `scripts/gsd/review-gate-command.sh` — gsd `workflow.code_review_command` target: autonomy-grant wall (fail-closed `ship:gsd`) + codex adversarial review (line-anchored verdict)
- `scripts/gsd/consent-check.sh` + `scripts/gsd/state-phase.sh` — deterministic gsd-state assertions (body-derived; frontmatter counters are known-unreliable upstream)
- `scripts/hooks/gsd-phase-evidence-gate.sh` — PreToolUse hook: ROADMAP/STATE phase-complete flips require gates.py evidence (host-side `verify:post`; capability-native gates non-functional at 1.6.1, upstream #2004/#2009)
- `templates/gsd-config.base.json` — base project config carrying both gate commands

### Changed
- `feature-implement` v2.0.0, `spec-decompose` v2.0.0, `fix` v3.0.0, `swarm` v2.0.0,
  `task-swarm` v2.0.0 — rewritten as thin `/gsd-*` wrappers keeping FFS walls
  (preflight, grant ledger, gates.py authority, review-gate); `feature-spec` chains
  the gsd-native decompose
- `setup.sh` — installs pinned gsd-core + gsd gate scripts (ruflo bootstrap removed)
- `docs/commands.md` — ruflo orchestration section replaced with the gsd loop table

### Removed
- `scripts/harness/ruflo-host-executor.{sh,bats}`, `ruflo-artifacts.sh`, `executor-detect.sh`
- `skills/agents-init/` (gsd install replaces roster scans)
- All `mcp__ruflo__*` callsites and `RUFLO_REQUIRED` plumbing; `dispatch.py` `fable-warn`
- `docs/ruflo-curation.md` content (superseded stub retained)

Evidence: `spike-results/gsd-ruflo/` (research report, Phase A pilot verdict PASS,
Phase B hooks note).

## v3.21.0 — OpenWiki living-docs wiring: conditional wiki auto-update (2026-07-04)

Consumer repos that keep a living wiki (`openwiki/` at repo root, reality/vision/gap
page layers) now get documentation-as-code for free; repos without one are
byte-identically unaffected (guard: `[ -d "$(git rev-parse --show-toplevel)/openwiki" ]`,
silent exit-0 no-op).

### Added

- **feature-spec 1.4.0 — Step 4.5 OpenWiki planned-change note.** After decompose,
  append a planned-change row (`- spec-<ID>: <title> — paths: … (noted <date>)`) to the
  mapped wiki page; unmappable page falls back to `openwiki/quickstart.md`. Fail-soft:
  any wiki error warns and continues — never blocks the spec pipeline.
- **feature-implement 1.14.0 — Step 10 finish-tail wiki stage.** Between review-gate and
  ship: refresh affected wiki pages from the run's diff (`git diff --name-only
  <base>...HEAD` mapping) and `git add openwiki/` so wiki updates land in the SAME
  branch/PR. Warn+continue on any failure — the wiki stage never blocks PR creation.
- **Extractable wiring blocks.** Both steps carry fenced bash between stable
  `<!-- openwiki-wiring:{spec-note,ship-stage}:{begin,end} -->` markers so consumer-repo
  harnesses can execute the exact shipped logic against fixture repos (with/without
  `openwiki/`) instead of trusting prose.
- **fix — explicit non-applicability note.** `/fix` never creates PRs (that's `/ship`),
  so it intentionally carries no wiki wiring; the note points to where the wiring lives.

## v3.20.0 — Runtime-proof phase QA: evidence-backed browser runthroughs + design review (2026-07-04)

Kills the "agent says it 200s, browser shows a 404" failure class. Browser and
design QA verdicts are now evidence-backed at the script layer — an agent's
"pass" without a verified proof bundle is rejected. Full doc:
`docs/browser-proof.md`.

### Added
- `lib/runtime_proof.py` — proof.json bundle verifier (completion authority
  for browser QA). Checks defeat named anti-patterns: curl-200-as-proof
  (content_assert + dom_excerpt required), soft-404 (marker scan on rendered
  DOM), wrong-page screenshot (url_final vs expect_url), post-hydration death
  (console_errors present + empty), static-frame-as-works (interactions >= 1
  for functional scenarios), stale/absent artifacts (screenshot exists +
  non-empty + fresh), self-report tier (`--strict` / `RUNTIME_PROOF_STRICT=1`
  rejects driver=agent). Canary driver cross-checks the session
  results.json. `skeleton` subcommand emits UNFILLED templates from
  scenarios.md. 56 pytest, RED-first.
- Adversarial hardening round (opus review, 3 HIGH fixed pre-merge):
  screenshot magic-byte image check (kills `printf x > shot.png`);
  `playwright` driver requires a fresh `playwright_artifact` (a bare
  `"driver": "playwright"` string no longer dodges strict mode);
  `proof.driver` cross-checked against the `browser-proof.txt` the resolver
  wrote (`--browser-proof` or auto-detected sibling); coverage completeness
  vs scenarios.md (`--scenarios`, or the `scenarios_source` the skeleton now
  embeds) — a bundle proving one easy scenario while the spec defines five
  is rejected; `gates.py analyze` machine-requires a `[qa:browser]`
  runtime-proof gate task in every story phase when scenarios.md exists
  (enforcement was opt-in prose before). Aggregate rejection now overwrites
  the dim result file to fail (no stale `"pass"` + inert `.rejected`
  sidecar), and runtime_proof.py resolves from
  `~/.claude/lib/feature-fix-swarm/` in the installed shape.
- Codex gate round 2 (1 CRITICAL + 2 HIGH fixed pre-merge): coverage
  requirement is now pinned by the CALLER via `--kind functional|visual|all`
  and keyed on scenarios.md source kinds — a forged bundle self-declaring
  everything "visual" can no longer shrink what proof.json must cover, and
  bundle-vs-source kind mismatches are findings (marking a functional flow
  "visual" to dodge the interactions check is caught); `gates.py analyze`
  requires the literal `runtime_proof.py verify` gate command, not a
  spoofable substring; WEB_RE broadened (any `app/` file, `hooks/`,
  `stores/`, `styles/`, framework configs — browser logic in plain `.ts` no
  longer slips past) with `QA_FORCE_BROWSER=1` as the explicit force lever.
- Codex gate round 3 (2 HIGH fixed pre-merge): `gates.py analyze` no longer
  lets a browser-touching plan bypass the whole lane by omitting
  scenarios.md — web-surface file paths in the tasks themselves
  (`WEB_TASK_PATH_RE`: UI extensions, UI dirs, framework configs) now demand
  scenarios.md + a `[qa:browser]` gate; scenario kind comes ONLY from an
  explicit `[visual]`/`[functional]` heading tag (untagged = functional,
  the strict default) — title-keyword inference could silently reclassify a
  functional flow titled "visual polish…" out of the `--kind functional`
  required set. 222 → 228 pytest. Round 4 (1 HIGH fixed): `WEB_TASK_PATH_RE`
  aligned with browser-proof.sh `WEB_RE` — hooks/stores/styles dirs +
  app/ api/ route files (path-with-extension anchored) count as web-touch
  at plan time exactly as at QA time. 228 → 231 pytest.
- `scripts/browser-proof.sh` — web-touch detection + base-url resolution
  (QA_BASE_URL authoritative; probe list fallback) + driver ladder
  (canary > playwright > agent, trust-descending). 12 bats.
- `prompts/qa-design.md` — design QA agent: visual review at 1440 + 375
  against `specs/NNN/design-intent.md` (extracted from the plan's
  /plan-design-review report) or DESIGN.md tokens; writes design-proof.json.
- `specs/NNN/scenarios.md` contract (spec-decompose v1.5.0) — BDD
  Given/When/Then with stable `US<N>-S<M>` IDs covering functional flows
  (buttons, forms, auth, navigation), executed 1:1 by the phase browser gate.
- Per-phase `[qa:browser]` gate task in the decompose grammar — checkbox only
  flips on `gates.py run-gate T### -- python3 lib/runtime_proof.py verify`.
- `[qa:design]` tag + UI-task QA default `[qa:e2e,browser,design,review]`.
- `docs/browser-proof.md` — schema, driver ladder, anti-pattern table,
  fail-not-skip semantics, OSS canary quickstart.

### Changed
- `scripts/qa-swarm.sh` — e2e dimension is now fail-not-skip: a web-touching
  diff with no reachable app FAILS the phase (old behavior silently skipped
  when :3000 didn't answer, letting UI phases pass QA with zero browser
  verification). `QA_ALLOW_NO_SERVER=1` is the only waiver, explicit and
  printed. New design dimension queued whenever the phase diff touches visual
  surfaces (no longer gated on /design-html tasks only). New `--aggregate`
  mode re-reads dim results without resetting them and REJECTS e2e/design
  passes whose proof bundles fail verification. 10 bats.
- `feature-implement` v1.13.0 — Step 5.5 rewired: browser-proof resolution
  runs BEFORE the QA swarm; qa-design is a 4th hive agent on UI phases
  (maker/checker: QA agents are fresh-context evaluators, never the
  implementer); `qa-swarm.sh --aggregate` is MANDATORY after agent verdicts
  in BOTH the hive and fallback paths (hive "pass" + aggregate exit 1 =
  phase QA FAIL).
- `spec-decompose` v1.5.0 — orchestrator merge contract emits scenarios.md +
  per-phase browser gates + design-intent extraction from the plan's
  GSTACK REVIEW REPORT.
- `feature-spec` v1.3.0 — verifies scenarios.md/design-intent.md exist for
  browser-touchable specs; preflight includes the qa-app-reachable probe when
  tasks.md carries `[qa:browser]` (unattended runs never reach phase QA
  without an app to verify against; prefer QA_BASE_URL = preview/prod build).
- `prompts/qa-e2e.md` — rewritten as an evidence contract: BDD scenarios via
  the resolved driver (canary sessions record trace/video/HAR/report.html),
  observe-before-act, per-scenario url_final/content/console/interaction
  capture, forbidden-moves list; silent scenario skips are FAILs.
- `setup.sh` — installs browser-proof.sh, qa-design.md, runtime_proof.py.

### Known limits
- The orchestration seams (Step 5.5 markdown, decompose merge contract) are
  outside pytest reach — pinned by the bats suites + skill-contract review.
- Agent-driver evidence is only as honest as its captured fields; strict mode
  (RUNTIME_PROOF_STRICT=1) is recommended for unattended runs where canary or
  playwright is installed.

## v3.19.0 — Swarm decomposition + agent roster + autonomous two-command pipeline (2026-07-04)

The pipeline collapses to two commands with ONE operator stop:
`/feature-spec NNN` → `/feature-implement NNN --autonomous`. New `/task-swarm`
does the same end-to-end from a free-text instruction.

- **Agent roster manifest** (`lib/agents_manifest.py` + `/agents-init` skill):
  scans `.claude/agents/**` frontmatter + `.codex/agents/*.toml` + optional
  seed file (`.feature-fix-swarm/agents.local.json` for plugin agents like
  `ecc:*` that are valid subagent_types but not files) + ruflo builtins +
  generic floor into `.feature-fix-swarm/agents.json` with domain buckets.
  Kebab-case dedup (codex `Brand Designer` == claude `brand-designer`).
  `check` subcommand validates every `[agent:X]` tag in a tasks.md against the
  roster (bare + `dept/role` forms), fails closed. setup.sh ships the module
  beside gates.py + best-effort scans at install. 16 tests, incl. real-repo
  regressions (greedy `auth` keyword, Title Case dupes).
- **spec-decompose v1.4.0 — swarm decomposition DEFAULT:** ruflo `swarm_init`
  (hierarchical) + orchestrator + per-domain specialists drawn from the roster
  propose task-subsets in parallel (native `Task()`, model-routed); a single
  orchestrator merges under the canonical decompose-spec.md grammar contract
  (dedup, cross-domain Depends-on, RED-before-GREEN, review-gates, roster
  `[agent:]` tags). `--no-swarm` / empty roster / no ruflo (auto) → legacy
  single-planner. Coherence gate now also runs `agents_manifest.py check`.
- **feature-spec v1.2.0 — full front half:** speckit specify→plan→clarify →
  spec-decompose → **preflight DEFAULT-ON** (`--no-preflight`) → **autonomy
  grant DEFAULT-ON** (`--no-grant`; one screen, typed actions, TTL'd — the
  `--autonomous` handoff never stalls ungranted). Prints the
  `/feature-implement NNN --autonomous` handoff.
- **feature-implement v1.12.0 — finish tail:** Step 10 review-gate → ship →
  canary as the terminal executor (`--no-finish` opts out); every outward
  action behind `check-grant` (autonomous) or explicit operator yes (attended);
  ungranted → `pending` + STOP, work stays local. Safety rules updated: push/
  deploy legal ONLY inside the gated tail. Step 9 retro optionally mirrors
  patterns to gbrain (fail-soft).
- **`/feature` RETIRED** → deprecated stub (3.0.0-deprecated) chaining
  feature-spec + feature-implement; removal target v3.20.0.
- **`/task-swarm` (new):** free-text task → plan-decompose (plan-eng-review +
  codex gates + swarm decompose) → preflight → grant screen →
  feature-implement --autonomous. session_save checkpoints per stage
  (long-run-continuity). One operator stop by contract.
- **gbrain optional integration** (`docs/gbrain-optional.md`): fail-soft
  detection contract (`command -v gbrain` + `env -u DATABASE_URL gbrain
  doctor`), per-phase usage (plan recall, decompose blast-radius via
  code-refs, retro put) with git/grep fallbacks; `gbrain init --pglite`
  quickstart for OSS consumers; gates.py stays gbrain-free.
- **Ruflo curation** (`docs/ruflo-curation.md`): adopted set documented
  (hooks_pre/post-task, model-route/outcome, session_save/restore, agentdb,
  hive-mind QA) + explicit NOT-adopted line (daa_*, autopilot_*, neural_*,
  workflow_execute, agent_execute) so future sessions don't cargo-cult.
- **Independent-review remediation (pre-merge):** opus tester found a CRITICAL
  RUN_ID seam break — feature-implement never set the ledger key and overloaded
  `$RUN_ID` with a run-state UUID, so grants written by feature-spec/task-swarm
  under `spec-NNN` were unreadable (autonomous runs refused to start or stalled
  every ship/canary gate). Fixed: `RUN_ID="spec-${SPEC_ID%%-*}"` at flag-parse
  (bare-numeric normalized in all three skills), run-state ids renamed
  `AUDIT_RUN_ID`, autonomous checks routed through resolved `$GATES_PY`,
  resolvers inlined in feature-spec/task-swarm, agents-init resolver gained the
  setup.sh-installed path. codex round 1 (FAIL→fixed): symlink-escape refusal in
  the roster scan (HIGH), unclosed-frontmatter injection killed, `[agent:]` tag
  grammar rejects `..`/deep slashes, same-source collision warning (cross-source
  mirror dedup stays silent), spec-decompose always rescans the roster. Suite
  92 → 114 tests. codex round 2 (MED, fixed): `RUN_ID` was assigned before the
  branch-derived SPEC_ID fallback, so no-arg `/feature-implement` invocations
  keyed the ledger as `spec-` — assignment moved below the fallback + guard
  (feature-implement v1.12.2). Residual LOW: the always-rescan and
  branch-derived-RUN_ID paths live in skill markdown, outside pytest reach.

## v3.18.0 — Autonomy grant ledger + preflight (2026-07-04)

Front-load run-time decisions to plan-time so unattended runs never stall.
Inspired by elias/fable-agent-orchestration `autonomous-finish-loop` +
`long-run-continuity` (Apache-2.0).

- **Autonomy grant ledger** (`gates.py grant` / `check-grant` / `pending`):
  at plan approval the operator pre-approves the run's operator gates as
  TYPED actions (`push:origin/main`, `deploy:vercel-web`, …). The loop checks
  the ledger mechanically (exit code) instead of stopping to ask. Grants are
  run-bound + TTL'd (default 24h), fail closed on expiry/mismatch. Unlisted
  gates STOP and write a durable `pending` record so the morning resume is
  one `grant` command. Free-prose actions rejected at grant time.
- **Preflight** (`gates.py preflight` / `check-preflight` + `/preflight`
  skill): prove env vars present (names only — secret values never enter the
  store) and services reachable (real probes, not greps) BEFORE an unattended
  run starts. Empty manifest fails. `check-preflight` requires a recorded
  PASS < 24h old.
- **`/autonomy-grant` skill**: gate enumeration → one-screen operator
  approval → recorded grant → mechanical checks; typed action vocabulary;
  novel-action safety floor kept (deliberately no wildcards).
- **feature-implement `--autonomous`**: refuses to start without a fresh
  preflight PASS; checks the ledger at every operator gate; logs consumed
  grants + artifacts in the final report; pendings listed for resume.
- setup.sh installs the two new skills (lever updated in same change).
- Codex round-1 hardening: bounded finite TTL (0 < h <= 168), future-dated
  preflight rejected (0 <= age), pending actions typed-validated, all
  operator-visible action/reason echoes sanitized. Threat model documented:
  the ledger is an anti-accident mechanism + intent record, not an
  anti-adversary boundary (check-grant confers no capability a shell agent
  lacks; same documented decision class as v3.14 no-HMAC).

## v3.17.0 — REFUTED outcome, verify-the-reviewer, WIP adoption (2026-07-04)

Ported from elias/fable-agent-orchestration (git.wearein.space, Apache-2.0) —
the review-verifier, investigate-before-fix / think-work-try result states,
orphaned-wip-adopter, and two-critic test-gate-critic primitives.

### Added
- **REFUTED as a first-class outcome** (`lib/gates.py note-refuted`,
  `skills/fix`, `skills/feature-implement`, `prompts/decompose-spec.md`).
  When a sub-agent proves the task's diagnosis wrong at current HEAD, the
  task closes with ZERO diff: `note-refuted <task> --reason "<evidence>"`
  records it, `verify-done` accepts it (prints `DONE-REFUTED`) so the
  checkbox-evidence hook passes, and the escalation ladder is SKIPPED — a
  correct refutation is a result, not a failure to retry. The refutation
  itself is adversarially checked via review-gate refute-or-promote before
  the checkbox flips via a two-step protocol: `note-refuted` records
  (confirmed=false) → review-gate refute-or-promote → `confirm-refuted`
  unlocks strict `verify-done` (`GATES_STRICT=1` fails CLOSED on an
  unconfirmed refutation — codex gate CRITICAL fix). Printed reasons are
  control-char sanitized (codex MEDIUM). Residual: `confirm-refuted` is still
  caller-executed (no runner-provable refutation exists); the two-step split
  makes skipping the adversarial check a distinct, auditable action.
- **`/verify-review` skill** (new, `skills/verify-review/`). Operator-facing:
  before acting on ANY review verdict (review-gate, codex review, PR review),
  spot-check the load-bearing claims against current HEAD and classify
  REAL_BLOCKER / REAL_NON_BLOCKING / STALE / WRONG / CONFIRMED_PASS. Never
  forward unverified reviewer claims to a fixer agent.
- **`/adopt-wip` skill** (new, `skills/adopt-wip/`). Salvage coherent
  uncommitted work from stalled worktrees: confirm stale → read diff →
  assess coherence → exercise the load-bearing claim → adopt/rebuild/wait.
  Adopted diffs get NO gate discount. Also referenced from
  feature-implement's resume semantics.
- **Test-gate-critic questions in review-gate Pass 3** (two-critic split):
  would each changed test FAIL under the old broken behavior; production
  path vs reimplementation; was a gate weakened to turn green; name the
  easy fake pass. Review-gate bumped to 1.2.0.
- **Verify-the-reviewer step in review-gate** (post-verdict): stale/wrong
  claims are discarded with recorded reasons; clean PASSes get their 1-2
  highest-cost claims spot-checked.
- **Fail-under-broken language in decompose-spec**: every test task states
  its trajectory and must fail under old behavior; name the easy fake pass.

## v3.16.0 — return contracts + escalation ladder (token discipline) (2026-07-04)

### Added
- **`[return:scout|build|deep]` task annotation** (`lib/dispatch.py`,
  `prompts/decompose-spec.md`). Bounds what a sub-agent RETURNS: scout =
  ≤15-line facts report (file:line refs, never paste file contents), build =
  ≤20-line change report (diffs only if ≤30 lines), deep = ≤40-line
  conclusion-first report. Default derives from model tier (low→scout,
  mid→build, high→deep) so existing tasks.md files need no changes; annotate
  only to override. Host-neutral — keyed off tier sets, works with or without
  fable, same on Codex (`gpt-5.4-mini`/`gpt-5.4`/`gpt-5.5`).
- **Return-contract + context-discipline sections in sub-agent prompts**
  (`skills/feature-implement/SKILL.md`, `skills/fix/SKILL.md`): failures-only
  test output ("N passed" on green), grep-before-read, read ranges not files,
  never re-read unchanged files, cite file:line instead of pasting contents.
  "Your final message IS the report — orchestrator reads reports, not
  transcripts."
- **Task-failure escalation ladder** (`skills/feature-implement/SKILL.md`,
  `dispatch.escalate_model()`): fail once → retry same tier with the failure
  report appended; fail twice → escalate ONE tier up (haiku→sonnet→opus;
  gpt-5.4-mini→gpt-5.4→gpt-5.5) carrying BOTH prior failure reports plus a
  `gates.py note-failure` signature; top-tier failure → mark `failed` and
  continue (no_progress STOP unchanged). In-family only — Codex hosts never
  escalate into Anthropic models.

### Rationale
Part-2 ("token discipline") of the fable-mode-derived orchestration charter.
FFS already had the Part-1 role charter (model routing, phase gates, evidence
verification); this release adds the missing output-side discipline so
sub-agents stop dumping raw transcripts into orchestrator context.

## v3.15.1 — remove codex-gate compat alias (2026-07-04)

### Removed
- **`skills/codex-gate/`** — the compatibility alias for `/review-gate`. It was
  a pure 5-line stub (no logic of its own) that existed only so pre-v3.13
  task files, docs, and muscle memory naming `/codex-gate` would not break.
  `/review-gate` has been the canonical host-neutral gate since v3.13.0; the
  alias is no longer needed and its continued presence was a source of "which
  one do I run" confusion.

### Changed
- `--no-codex-gate` / `--skip-codex-gate` flags (and their `NO_CODEX_GATE` /
  `SKIP_CODEX_GATE` shell variables) renamed to `--no-review-gate` /
  `--skip-review-gate` (`NO_REVIEW_GATE` / `SKIP_REVIEW_GATE`) in `skills/fix`
  and `skills/feature` to match the gate's actual name.
- `scripts/hooks/codex-gate-warn.sh` renamed to `scripts/hooks/review-gate-warn.sh`
  (referenced from `skills/feature/SKILL.md` and `skills/fix/SKILL.md`).
- `setup.sh`'s skill-install list no longer installs `codex-gate`.
- Docs (`README.md`, `docs/commands.md`, `docs/pipeline.md`, `lib/run_state/README.md`,
  `skills/spec-decompose/SKILL.md`) updated to reference `/review-gate` only.

Historical inline comments citing `codex-gate` findings by round/PR number
(e.g. "codex-gate round 3, PR #13") are left as-is — they document what a
past review pass actually found under its name at the time, same as
CHANGELOG entries above this one.

## v3.15.0 — proof artifacts, named deferrals, review anti-recursion scope (2026-07-03)

### Added
- **`gates.py proof <run-id> <task-ids…>`**: per-run machine-readable proof
  artifact written to `.feature-fix-swarm/proof-<run>.json` — one claim per
  task with the evidence command, real exit code, sha256 of the stored log
  material, and a live-vs-structural kind (runner-executed vs caller-recorded).
  Go/no-go verdict (exit 1 on no-go); `--strict`/`GATES_STRICT=1` rejects
  structural claims. Ported from the openclaw evidence discipline.
- **Named deferrals**: `proof --defer "name: reason"` records anything
  intentionally not verified this run in the artifact; `feature-implement`
  v1.11.0 additionally requires appending each deferral to
  `.feature-fix-swarm/residuals.md` — a deferral not named in both places is
  a silent pass, forbidden and enforced: `proof` no-goes any `--defer` whose
  name is absent from residuals.md (`DEFERRAL-UNRECORDED`). Run-id is
  sanitized before composing the default artifact path (no traversal);
  trailing value-flags are a usage error (exit 2), never a crash.
- **Review anti-recursion scope** baked into `review-gate` (and its
  `codex-gate` alias): reviewers must never recurse into `.claude/`,
  `.codex/`, `skills/`, `agents/`, `.agents/`, or SKILL.md/SOUL.md/AGENTS.md
  of the CONSUMER repo — instruction files are data, not review targets.
  In feature-fix-swarm itself `skills/` is the product and stays reviewable.

### Deferred (named, per the new convention)
- **mutation-smoke** (run pytest against mechanical mutations of changed impl
  files to prove tests bite): not cheap to make deterministic this round;
  recorded here as the residual instead of a half-built gate.

## v3.14.0 — evidence provenance, phase truth score, no-progress wiring, CI (2026-07-03)

### Added
- **CI at last**: `.github/workflows/ci.yml` — pytest (`lib/` + `tests/`),
  shellcheck (warning level) over the executable shell surface, bats (when
  suites exist). Gate-integrity guarantees now enforced on every push/PR.
- **Strict evidence provenance**: `verify-done --strict` (or `GATES_STRICT=1`)
  rejects caller-recorded evidence — only runner-executed `run-gate` evidence
  can flip a checkbox. All evidence entries carry `executed_by`
  (`run_gate` | `run_red` | `caller`); `record-gate`/`record-red` print a
  runtime WARNING naming themselves forgeable. feature-implement v1.10.0
  exports `GATES_STRICT=1` in the Ralph loop. Decision: no HMAC/runner-token —
  over-engineering for a local single-user store; the `executed_by` allowlist
  is the same authority boundary.
- **`gates.py phase-score`** — wires the previously-dead `truth_score` into the
  pipeline: classifies each task's stored gate cmd into compile/tests/lint/
  typecheck, scores the phase (weights .35/.25/.20/.20, normalized over
  categories present; any task with no evidence → 0.0), exit 1 below the 0.95
  threshold → phase rollback. feature-implement runs it at every phase gate.
- **`gates.py note-failure`** — wires `no_progress` into the retry loop via the
  evidence store: exit 1 when the same failure signature repeats twice in a
  row → the loop STOPs for human review instead of burning retries.
- **feature-implement Step 9 retro** — end-of-run learning consolidation:
  aggregate gate outcomes from the evidence store, distill ≤3 reusable patterns
  to agentdb, append a run-summary line to `results.md`.

### Fixed
- **CHANGELOG numbering**: two entries both claimed v3.12.0 (machine gates and
  the openclaw-fork reconcile). The reconcile entry — changelog union, patch
  scope — renumbered **v3.11.1**; machine gates keeps v3.12.0 (matches merged
  PR #13 title). Entries re-sorted by version; missing PR #14 entry backfilled
  as v3.13.2.
- `scripts/ralph-retry.sh`: removed dead `QA_RERUN_EXIT` (shellcheck SC2034)
  so the CI shellcheck job starts green.

### Skills
- feature-implement 1.9.0 → **1.10.0** (GATES_STRICT, phase-score rollback,
  note-failure stop, Step 9 retro).

## v3.13.2 — setup.sh non-interactive deploy + self-copy guards (2026-07-03)

(Backfilled entry for PR #14, which shipped without one.)

### Fixed
- `setup.sh --yes` / `FFS_SETUP_YES=1` non-interactive mode — the overwrite
  prompt exited 1 with no TTY, silently leaving old skill versions installed
  (found by post-deploy canary).
- `-ef` self-copy guards on the four CWD copy loops (scripts/, scripts/harness/,
  scripts/hooks/, prompts/) — setup.sh from the repo root no longer dies on
  `cp: … are identical`.

## v3.13.1 — review-gate Pass 2 CLI-flag fix (2026-07-03)

### Fixed

- **`review-gate/SKILL.md` Pass 2 (adversarial, cross-model dispatch)**: both
  the Claude and Codex branches used a bare `--system` flag that doesn't
  exist on either CLI (verified against `codex` v0.142.5's and current
  `claude`'s own `--help`) — the invocation was silently swallowed by its own
  `|| echo "[...skipped]"` fallback, so Pass 2 has likely never actually run
  against a current-generation CLI. Fixed:
  - Claude branch: `--system` → `--system-prompt` (the actual flag name).
  - Codex branch: `codex review` in this version is agentic against the live
    repo (it runs its own `git diff` inside its sandbox) rather than a
    pipe-diff-in/get-text-out tool, and `--base`/`--commit` can't combine
    with a custom `PROMPT` on this CLI. Rewrote to hand it a natural-language
    description of the diff scope instead of piping `$DIFF` directly.
  - Also dropped `--model gpt-5.4` (not a valid `codex review` flag; model
    override is `-c model=...` and gpt-4o-class models aren't supported on
    ChatGPT-account plans) — left unset to use the account's default model.
  - Verified end-to-end: ran the exact committed bash block against a real
    test diff on both branches, clean exit 0, correct diff scoping, real
    findings-format output.

## v3.13.0 — goal-wrap skill + codex-gate hardening (2026-07-03)

### Added

- **`skills/goal-wrap/SKILL.md`** — new skill, ported from the openclaw vendored
  fork. Bundles current session state into a self-contained, anti-drift
  `/goal "..."` prompt with tracked `DONE WHEN` proof commands and a research
  pass. Adapted for standalone use: the original's hard dependencies on
  `repowise`/`gbrain`/`/prompt-master` now have documented grep/git/inline
  fallbacks (see the skill's own "Soft dependencies" table); `/handoff`
  already degraded gracefully in the source. `setup.sh` now installs it.
- `docs/commands.md` — added missing `/plan-decompose`, `/swarm`, and
  `/goal-wrap` rows (all three were real, shipped skills with no doc entry).
- `README.md` "What's in the box" — refreshed from a stale 5-skill/3-doc
  inventory to the actual 10-skill/4-doc/lib tree.

### Fixed (codex-gate 3-pass review of PR #11)

- **CRITICAL** — `feature-implement/SKILL.md`, `swarm/SKILL.md`: `DISPATCH=`
  hardcoded an openclaw-monorepo-only path
  (`packages/feature-fix-swarm/lib/dispatch.py`); `setup.sh` never installed
  `dispatch.py` anywhere, so every standalone install silently broke task
  parsing. Fixed with a 3-way fallback (openclaw-vendored → installed at
  `~/.claude/lib/feature-fix-swarm/` → standalone repo root); `setup.sh` now
  installs `dispatch.py` alongside `run_state`.
- **HIGH** — `lib/dispatch.py` `route_agent()`: `api-documenter` was
  unreachable dead code (`docs-architect`'s bare `docs` keyword
  substring-matched inside "api docs"/"developer docs"/"openapi docs" and was
  checked first). Reordered; added regression coverage.
- **HIGH** — `swarm/SKILL.md`: one non-goals line had regressed to legacy
  `/codex-gate` naming while every sibling file in the same reconciliation
  was renamed to `/review-gate`.
- **HIGH** — `prompts/decompose-spec.md`, `spec-decompose/SKILL.md` (7
  occurrences): the canonical `/review-gate` task template tagged `[P]`
  while also carrying `Depends-on:` — self-contradictory with this doc's own
  `[P]` definition. Dropped `[P]` from the template.
- **MEDIUM** — stale `gpt-5.3-codex-spark` references in README.md and
  `prompts/decompose-spec.md` (the tier was renamed to `gpt-5.4-mini` in
  `ruflo-host-executor.sh` in the same PR); a swallowed `reset --hard`
  failure in `setup.sh`'s pack-sync now falls back to a fresh clone.
- **LOW** — redundant `re.IGNORECASE` in `route_agent()`; unanchored
  `## Phase N` grep in `feature-implement/SKILL.md` (matched `Phase 10`,
  `Phase 11`, etc. for `Phase 1`).

Deferred (documented, not blocking): Pass 3 flagged 6 HIGH test-coverage
gaps — most `AGENT_ROUTING_RULES` entries untested, zero test file for
`setup.sh`, and the `qa-swarm.sh` aggregation fix landed without a
regression test. Real technical debt, tracked as follow-up.

## v3.12.0 — machine gates: human-out-of-loop hardening

**Grammar fix (CRITICAL):** `[qa:]` parser char class could not match digits or
hyphens — `[qa:e2e]` and the reserved `[qa:review-gate]` phase-gate tag silently
fell back to default dims. Fixed (`[a-z0-9,-]`) + regression tests.

**New `lib/gates.py`** (installed by setup.sh next to dispatch.py):
- `record-gate`/`verify-done` — checkbox `[X]` flips now require recorded gate
  evidence (exit 0 + test counts); agent self-report is never completion authority.
- `record-red`/`check-red` — GREEN tasks blocked until a real failing-test RED
  proof is stored (all-green logs rejected).
- `scan-tamper` — reward-hacking guard: deleted asserts, added skips, `exit 0`,
  CI-config edits flagged CRITICAL.
- `analyze` — spec↔tasks coherence gate (spec-kit analyze analog): US coverage,
  per-phase review-gate task, per-story e2e smoke task.
- `truth_score` (compile .35/tests .25/lint .20/typecheck .20, 0.95 threshold →
  checkpoint rollback), `no_progress` (repeated failure signature → stop),
  `GATE_LADDER` (compile→…→e2e→review, fail-fast).

**Emitter (decompose-spec):** RED-proof pairing mandatory (every impl task
depends on a failing-test task); every story phase must end with an e2e smoke
task derived from BDD scenarios; self-check now runs `gates.py analyze`.

**review-gate:** refute-or-promote pass — HIGH/CRITICAL findings block only
after surviving one adversarial refuter (false-positive control for autonomous
runs); LLM review rounds capped at 2/phase.

**Hooks:** optional `hooks/tdd-gate.sh` PreToolUse hook (block source writes
with no matching test; `TDD_GATE_BYPASS=1` when authoring the test).

**Portability:** zsh-safe argument loops in review-gate + feature-spec
(command-substitution split — verified identical in bash and zsh).

**Docs:** full 46-agent routing catalog in commands.md + silent
general-purpose-fallback warning; gate-ladder section in qa-ralph-loop.md.

All notable changes to feature-fix-swarm are documented here.

## v3.11.1 — reconcile openclaw vendored fork with OSS canonical (2026-07-03)

The openclaw vendored copy (`packages/feature-fix-swarm/`) and the OSS canonical
repo diverged after v3.5.0 with no shared git history since. Both sides added
real, independent entries. This release is a pure changelog reconciliation —
no functional merge of the underlying skill files was performed here, only
the historical record was unioned so no entry from either side is lost.

**Merged in from the openclaw vendored fork (v3.6.0 → v3.8.1):**
- `feature-spec` skill — new skill that creates `specs/NNN/spec.md` + `plan.md`
  from a GitHub issue, Linear ticket, or freeform description
- `/swarm` skill — ad-hoc, no-spec-directory task swarm executor with
  classification, parallel dispatch, and Ruflo coordination
- Canonical cross-host task format (`haiku`/`sonnet`/`opus` ladder) in
  `prompts/decompose-spec.md`, shared by Claude Code and Codex
- `setup.sh` upstream freshness checks for ECC and `wshobson/agents` packs

**Merged in from the OSS canonical repo (v3.6.0 → v3.11.0, renumbered v3.9.0 → v3.11.0 below):**
- Hybrid exact-agent routing — `[agent:exact-agent]` annotations against a
  comprehensive ECC + wshobson catalog, replacing department/role fallback
- Codex Ruflo discovery fix — pre-flight now uses `mcp__ruflo__mcp_status`
  instead of the stale `swarm_status` probe, with lazy tool discovery
- `fable` tier — optional 4th tier on the model ladder for multi-file
  narrative/voice-coherence tasks, plus `/review-gate` hang/timeout guidance

**Renumbering note:** both forks independently reused the version number
`v3.6.0` for unrelated content (openclaw: *feature-spec skill*, 2026-06-22;
OSS canonical: *hybrid exact-agent routing*, 2026-06-30). The vendored fork's
`v3.6.0` keeps its original number below (it is chronologically first and
matches the openclaw repo's on-disk history). The OSS canonical's colliding
`v3.6.0` has been renumbered to `v3.9.0` in this merged history — content is
otherwise unchanged. OSS canonical's `v3.10.0` and `v3.11.0` did not collide
and are unchanged.

## v3.11.0 — fable tier for multi-file narrative coherence (2026-07-01)

### Added

- **`prompts/decompose-spec.md`** documents `fable` as an optional 4th, Claude-Code-native
  tier on the model ladder (`haiku` / `sonnet` / `opus` / `fable`), reserved for multi-file
  narrative/voice-coherence tasks. No Codex equivalent; downgrades to `sonnet` on the
  Ruflo-coordinated path.
- **`skills/spec-decompose/SKILL.md`** report box and suspicious-output checks now cover
  `fable` (distribution line + over-escalation flag, mirroring the existing `opus` check).
- **`README.md`** Host-aware routing section documents the `fable` tier and its fallback
  behavior.
- **`skills/feature/SKILL.md`** and **`skills/fix/SKILL.md`** — `/review-gate` now has an
  explicit note on the correct behavior when it hangs or times out (structured blocked
  gate, no first-person narration of the failure).

## v3.10.0 — Codex Ruflo discovery and live MCP pre-flight (2026-07-01)

### Fixed

- **`skills/feature-implement/SKILL.md`** — Ruflo pre-flight now uses `mcp__ruflo__mcp_status` instead of the stale `swarm_status` probe. Codex runs now explicitly lazy-load Ruflo tools via tool discovery before deciding Ruflo is unavailable.
- **`skills/swarm/SKILL.md`** — ad-hoc swarms now document Codex lazy tool discovery before falling back away from Ruflo.
- **`docs/commands.md`** — documents `mcp_status` as the Ruflo health check and calls out Codex lazy tool discovery.

## v3.9.0 — hybrid exact-agent routing and installer freshness checks (2026-06-30)

> Renumbered from OSS canonical's `v3.6.0` during the 2026-07-03 changelog
> reconciliation to resolve a version-number collision with the vendored
> fork's own `v3.6.0` (feature-spec skill, 2026-06-22, unchanged below).
> Content is otherwise identical to the OSS canonical entry.

### Changed

- **`prompts/decompose-spec.md`** now uses `[agent:exact-agent]` and a much more
  comprehensive ECC + wshobson catalog so decomposed specs can be routed to the
  best-fit specialist instead of the old department/role fallback.
- **`skills/feature-implement/SKILL.md`** now parses exact labels, maps the hybrid
  catalog to cognitive patterns, and defaults to `general-purpose` only when a task
  omits the agent annotation.
- **`skills/spec-decompose/SKILL.md`**, **`docs/commands.md`**, and **`docs/pipeline.md`**
  now document the exact-agent routing contract so the decomposition and execution
  instructions stay in sync.
- **`setup.sh`** now checks the upstream `main` SHA for **ECC** and **wshobson/agents**
  and refreshes those packs when they drift, alongside the existing gstack/spec-kit
  bootstrap flow.

## v3.8.1 — external agent pack freshness checks (2026-06-30)

### Changed

- **`setup.sh`** now checks ECC and `wshobson/agents` against their upstream
  `main` HEAD, installs them when missing, and refreshes the local install when
  the recorded commit drifts from upstream.
- **`README.md`** now documents the ECC and `wshobson/agents` bootstrap check so
  the agent-pack dependency behavior is discoverable.

## v3.8.0 — canonical cross-host task format (2026-06-30)

### Changed

- **`prompts/decompose-spec.md`** now emits the shared canonical task ladder
  (`haiku` / `sonnet` / `opus`) in `tasks.md` for both Claude Code and Codex.
- **`skills/swarm/SKILL.md`** now classifies ad-hoc tasks into the canonical
  ladder and resolves those tiers to the active host at execution time.
- **`skills/spec-decompose/SKILL.md`** now treats host-specific model IDs as
  executor aliases only; decomposition output stays host-neutral.
- **Docs updated** to describe `tasks.md` as a canonical intermediate format
  usable by either runtime, with Codex runtime IDs handled during execution.
- **Parser regression coverage** now verifies both canonical tiers and legacy
  Codex model IDs still parse for older hand-edited task files.

## v3.7.0 — /swarm skill: ad-hoc task swarm executor (2026-06-28)

### Added

- **`skills/swarm/SKILL.md` v1.0.0** — new skill that accepts natural-language task
  descriptions (inline args or `--tasks-file`), classifies them via a Sonnet sub-agent,
  and executes them through Ruflo coordination + native `Task()` (Claude Code OAuth-only;
  `mcp__ruflo__agent_execute` is never called).

  Key features:
  - **No spec directory required.** Unlike `/feature-implement`, you don't need a
    `specs/NNN/tasks.md`. Pass tasks directly: `/swarm "write tests" "fix lint"`.
  - **Classification agent** (Sonnet `Task()`) assigns `[model:haiku/sonnet/opus/fable]`,
    `[agent:TYPE]`, `[thinking:low/med/high/max]`, and `[P]` (parallel-safe) annotations
    using the same heuristics as `/spec-decompose`.
  - **Parallel dispatch** — `[P]`-marked tasks fire as concurrent `Task()` calls with
    `run_in_background: true` in a single message turn.
  - **Ruflo coordination** — `swarm_init` + `agent_spawn` for metadata; `memory_*` +
    `agentdb_*` + `hooks_*` for pattern learning. Ruflo pre-flight auto-fallback: if
    `swarm_status()` fails, switches to native parallel path with no exit.
  - **Thinking alignment** — opus+med→high, haiku+high/max→med (prevents cost mismatch).
  - **Fable support** — fable tasks execute via native `Agent` path (not Ruflo, whose
    model enum is `haiku|sonnet|opus|inherit` only).
  - **Dry-run** (`--dry-run`): classify + print annotated plan + cost estimate, exit 0.
  - **`--tasks-file PATH`**: read raw tasks from a markdown checklist or one-per-line file.
  - **`--swarm-id ID`**: resume an existing run (skip classification if `tasks.md` exists).
  - **Self-critique before report**: names tasks done by self-report only (no file artifact).
  - **Run state**: `.context/swarm/<run_id>/tasks.md` + `run.log` (JSONL).

  Flags: `--dry-run`, `--sequential`, `--model-override MODEL`, `--no-memory`, `--no-auto`,
  `--tasks-file PATH`, `--swarm-id ID`.

  Non-goals (v1): no QA loop, no codex-gate, no commit/push, no task-level retries.

## v3.6.0 — feature-spec skill (2026-06-22)

### Added

- **`skills/feature-spec/SKILL.md` v1.0.0** — new skill that creates `specs/NNN/spec.md`
  and `plan.md` from a GitHub issue URL, Linear ticket, or freeform description.
  Fills the pipeline gap before `/spec-decompose` (which requires `plan.md`) and
  `/feature` (which requires `plan.md`). Spawns two Sonnet sub-agents: one writes
  the spec (user stories + acceptance criteria), one writes the plan (phases, tech
  stack, risks). Handles GitHub issue ingestion via `gh issue view`.

## v3.5.0 — Fable-mode operating disciplines (2026-06-13)

Inspired by [Fable-mode](https://github.com/mrtooher/fable-mode), which encodes three
behaviors that make agentic pipelines reliable: **decompose before acting, verify before
advancing, self-critique before delivery.** feature-fix-swarm already enforces the middle
one (QA-per-phase is *verify before advancing*). This release adds the bookends.

### Changed

- **`skills/feature/SKILL.md` v2.3.0 → v2.4.0**
  - New **"Operating disciplines (Fable-mode)"** section: stage-map-first, verify-before-advancing
    (credits the existing per-phase audit + `/codex-gate` as the embodiment), self-critique-before-delivery.
  - New **Step 9.9: Self-critique before delivery** — re-read the run as a skeptic before the
    final report; name ≥1 residual risk (audit-vs-self-report gaps, retry-only passes, happy-path-only
    verification, downgraded codex-gate findings). Fix it or surface it.
  - Final report gains a `Residual risk:` line so "all green" is never silent.
- **`skills/feature-implement/SKILL.md` v1.5.0 → v1.6.0**
  - New **"Operating disciplines (Fable-mode)"** section mapping the same three disciplines onto
    task execution (stage-map = phase plan after Step 2; verify = `--qa-loop` phase gate; self-critique
    before Step 8).
  - **Delegation discipline** callout in Step 5: parallelize only on genuinely independent stages
    (disjoint files, no shared mutable state, no ordering dep). When unsure, run sequentially.
  - Step 8 gains a self-critique preamble + `Residual risk:` report line.

### Not changed

- No behavior is removed or gated differently — all additions are advisory disciplines layered on
  the existing pipeline. The QA-per-phase enforcement, retry loop, and gates are untouched.

### Why minor (not patch)

Adds new workflow steps (self-critique) and new operator-visible report fields across two skills.

## v3.2.0 — feature-implement v1.2.0 ruflo schema alignment (2026-05-27)

### Discovered during spec-137 dogfood

`/feature-implement --ruflo` (v1.1.0) prescribed `mcp__ruflo__task_create({model, agent_role, depends_on})`. The actual ruflo MCP schema accepts only `{type, description, priority, assignTo, tags}` — no `model`, no `agent_role`, no `depends_on`. Per v1.1.0's own "hard-fail on schema mismatch" policy, every `--ruflo` run since the policy landed has been one tool-call away from `exit 1`.

### Changed

- **`skills/feature-implement/SKILL.md` v1.1.0 → v1.2.0** — Rewrote the "Ruflo path" block to match the live ruflo MCP schema:
  - `swarm_init` uses `maxAgents` (camelCase) + `strategy: "specialized"` + `config: { consensus }` (not top-level `max_agents`/`consensus`)
  - One `agent_spawn` per unique `[agent:dept/role]` (model selection happens here, NOT at task_create)
  - Single-model-per-agent constraint: if two tasks share an agent role but specify different models, spawn one agent per (role, model) tuple
  - `task_create` puts model/thinking/phase/US/qa metadata in `tags`, agent assignment in `assignTo`
  - Dependencies expressed via `workflow_create` step graph with `dependsOn` in step `config`, plus `parallel` step type for `[P]` groups
  - Added annotation→ruflo field mapping cheat-sheet

### Not changed

- Native Agent fallback path (`RUFLO_REQUIRED=0`)
- tasks.md annotation format
- Per-phase QA gate workflow
- Cost estimation block

### Why minor (not patch)

Schema-breaking change to the ruflo orchestration contract. Consumers who memorized the old `task_create` shape need to update integrations.

## v3.1.1 — Fix v3.1.0 architectural defect: /goal can't be invoked from skill (2026-05-12)

### Discovered during dogfood QA

v3.1.0 claimed `/feature` would auto-invoke native `/goal` via `Skill { skill: "goal", args: "..." }`. **That call path is architecturally impossible** — Claude Code's Skill tool refuses with `"goal is a UI command, not a skill. Ask the user to run /goal themselves — it cannot be invoked via the Skill tool."` UI commands like `/goal`, `/clear`, `/resume` are operator-typed input, never LLM-callable.

### Changed

- **`skills/feature/SKILL.md` v2.1.0 → v2.1.1** — Step 0.5 now PREPARES the goal condition (still bakes `run_id` into it for grep-correctness) and PRINTS a copy-paste banner. Operator pastes the `/goal "..."` line as their next message. Skill explicitly warns Claude NOT to attempt the Skill-tool invocation that v2.1.0 prescribed.
- **Event renamed** in `$RUN_LOG`: `step: "goal-set"` → `step: "goal-prepared"`, `status: "set"` → `status: "awaiting-operator-paste"`.
- **`Native /goal — auto-managed (v2.1.0+)`** section in SKILL.md renamed to **`Native /goal — operator-set, skill-prepared (v2.1.1)`**.

### Not changed

- Step 0 flag parsing (`--no-goal`, `--dry-run`, `--resume`, etc.)
- Run-state record creation in Step 0.5
- GOAL_COND composition with `run_id` baked in
- `--dry-run` and `--resume` guards
- Step 4b per-phase audit + Step 5.7 /codex-gate
- Tests: 39/39 green (no test changes — banner is skill instruction text)

### Why patch (not major)

Internal mechanism fix. Operator-visible contract: same shape (paste `/goal "<prepared condition>"`) — only difference is operator types the paste manually instead of Claude doing it. Skill still does all the prep work that made v3.1 valuable.

### Source

QA dogfood subagent on macbook 2026-05-12 — failed at Step 0.5 with `"goal is a UI command, not a skill"` runtime error. Pipeline aborted before ruflo phase; ruflo MCP health probe succeeded so ruflo *would have been* callable but live verification deferred to re-run after this fix.

## v3.1.0 — /feature auto-sets native /goal (2026-05-12)

### Changed

- **`skills/feature/SKILL.md` v2.0.0 → v2.1.0** — skill now auto-invokes `/goal` at entry (new Step 0.5). Operator runs `/feature NNN` and the continuation loop is fully managed. Before v2.1.0 the operator had to manually `/goal "<condition>"` first; that was friction.

### Added

- **Step 0.5 — Create run-state record + auto-invoke `/goal`** inside `/feature` workflow:
  1. `run-state start` captures a `run_id` for this pipeline run
  2. Skill composes a `/goal` condition with the `run_id` baked in, referencing `~/.claude/state/audits.jsonl` for verdict grep, `/codex-gate` verdict in run events, and canary HTTP 200 (or `--no-canary` opt-out)
  3. Claude invokes the native `/goal` via the `Skill` tool
- **`--no-goal` flag** — opt out of skill-managed `/goal` invocation. Use when operator pre-set a custom `/goal` condition they want preserved, or running under `claude -p` where /goal is moot.

### Why minor (not major)

Adds a capability without breaking existing flow. The pre-v2.1.0 workflow (operator manually sets `/goal`) still works — they just add `--no-goal` and Claude won't touch their existing goal. Default behavior changes from "skill assumes operator set /goal" to "skill sets /goal automatically", but the runtime contract for `/feature NNN` semantics is unchanged.

### Note on /fix

`/fix` was NOT updated in this release. To get the same ergonomics for `/fix`, set the goal manually:

```
/goal "bug fixed: latest run-state audit --kind fix verdict=pass AND qa green"
/fix "..."
```

If you want `/fix` to auto-invoke `/goal` too, file an issue.

## v3.0.0 — Native /goal integration; strip Stop hook + marker (2026-05-12)

**Breaking change.** Anthropic shipped native `/goal` in Claude Code 2.1.139+ ([docs](https://code.claude.com/docs/en/goal)). It owns the continuation loop — a small/fast model checks the goal condition after every turn and auto-continues. Our Stop hook, marker file, continuation-count tracking, pause/resume, and budget_limited state are now redundant. ~300 lines of dead code removed. Run-state lib retains adversarial audit (`run-state audit --kind {fix, phase, feature}`) + /codex-gate — those are the cross-model verification value-add native /goal does not provide.

### Removed

- **`scripts/hooks/run-state-stop.py`** — Stop hook with XML-escape continuation, missing[] injection, budget-summarize prompt. Native /goal replaces.
- **`scripts/hooks/run-state-session.py`** — SessionStart hook anchoring CLAUDE_SESSION_ID. Native /goal tracks session state.
- **`lib/run_state/marker.py`** — `.active-run` marker file. Native /goal indicator replaces.
- **`lib/run_state/tests/{test_marker,test_stop_hook,test_session_hook}.py`** — 23 tests removed (coverage no longer applies).
- **`state.py`:**
  - `continuation_count` + `max_continuations` columns
  - `paused` + `budget_limited` states from VALID_STATES
  - `inc_continuation` method
  - Auto-flip-to-budget_limited in `inc_tokens` (still emits `budget_limit_hit` event for analytics)
- **`cli.py`:**
  - `cmd_pause` + `cmd_resume` commands (replaced by `/goal clear`)
  - All marker `set` / `clear` calls
  - `MarkerFile` import
- **`setup.sh`:** entire "Install Claude Code hooks" block + `~/.claude/settings.json` hook registration via `jq`.
- **Skills:** "Run-state lifecycle (mandatory)" preamble removed from both `/feature` and `/fix` SKILL.md.

### Changed

- **`skills/feature/SKILL.md` v1.4.0 → v2.0.0** (major bump):
  - New "Native /goal entry" section instructs operator to run `/goal "<condition>"` before invoking `/feature`
  - Audit verdicts written to `~/.claude/state/audits.jsonl` so the `/goal` condition checker can grep "all audits pass + codex-gate PASS + canary green"
  - Step 4b per-phase audit + Step 5.7 /codex-gate retained — only the run-state lifecycle wrapper is gone
  - `--tokens` flag dropped (no enforcement layer remains); `--skip-codex-gate` kept as emergency bypass
- **`skills/fix/SKILL.md` v1.4.0 → v2.0.0**: same restructure — native /goal entry + retained adversarial audit + retained codex-gate
- **VALID_STATES**: `("active", "pending_audit", "complete", "failed", "aborted")` — 5 states (was 7)

### Tests

- 36 pytest tests in `lib/run_state/tests/` (was 59 in v2.1). 23 deleted (marker + stop_hook + session_hook + continuation/budget_limited cases); 4 modified (marker assertions removed). All green.

### Migration

Existing v2.x DBs at `~/.claude/state/runs.db` keep their extra columns and any rows in dropped states. SQLite tolerates extra columns on SELECT. `run-state list` will show stale `paused`/`budget_limited` rows; abort if desired. New `start` calls use the slim schema.

If you had a v2.x session-pinned run that depended on the Stop hook to continue, manually set a native `/goal` before resuming work:

```
/goal "<condition that should hold when work is done>"
```

### Why major bump

Lifecycle contract changed. Skills no longer require `run-state start` to anchor lifecycle. Any external script that grepped Stop-hook continuation prompts is broken.

## v2.1.0 — Per-phase audit + cross-model gate (2026-05-12)

### Changed

- **`skills/feature/SKILL.md` v1.3.0 → v1.4.0** — pipeline restructure:
  - REMOVED end-of-pipeline `--kind feature` spec-completion audit (duplicative with `/autoplan`'s planning-time audit)
  - ADDED Step 4b — per-wedge adversarial audit via `run-state audit --kind phase` between every implemented wedge. Scope = THIS wedge only. Auditor's `missing[]` from a fail re-enters that wedge's implement loop.
  - ADDED Step 5.7 — mandatory `/codex-gate` cross-model review before `/ship`. Three Codex GPT-5 passes (review + adversarial-chaos + adversarial-test-gaps) against the full branch. PASS = proceed; BLOCK = fix inline and re-run.
  - Flags table: `--no-audit` removed (replaced by per-phase + codex-gate); `--skip-codex-gate` added for emergency-merge fallback

### Added

- **`lib/run_state/prompts/phase_audit.txt`** — hostile prompt template for per-wedge audit. Variables: `{{PHASE_NAME}}`, `{{PRIOR_PHASES}}`, `{{PHASE_SPEC}}`, `{{PHASE_DIFF}}`.
- **`cli.py cmd_audit --kind phase`** — third audit mode. On pass: state stays `active`, marker preserved (more wedges may follow). On fail: state reverts to `active`, audit_attempts++, missing[] persisted to events.
- **`cli.py _parse_tokens()`** — K/M/B/T suffix parsing for `--tokens` flag (`250K`, `1.5M`, `1B`, `2T`). Case-insensitive, decimal-friendly.
- **`scripts/hooks/run-state-stop.py` v2.1 — three upgrades ported from balakumardev/claude-code-goal:**
  - **R1 XML-escape objective** in `<untrusted_objective>...</untrusted_objective>` wrapper. Prompt-injection defense.
  - **R2 `missing[]` injection** — when state=active AND last_audit_verdict=fail, the most recent `audit` event's `missing[]` is appended to the continuation prompt as explicit TODOs.
  - **R3 budget-limited summarize prompt** — was: allow stop. Now: block + emit summarize/commit-WIP/resume-or-abort prompt.

### Tests

- 58 pytest tests in `lib/run_state/tests/` (was 43 in v2.0). 16 added; 1 contract-changed test deleted and replaced.
- New `tests/test_token_parser.py` (10 cases); `test_stop_hook.py` extended (R1/R2/R3 coverage); `test_cli.py` extended (phase audit pass/fail).

## v2.0.0 — Persistent run-state (2026-05-11)

### Added

- **`lib/run_state/` — new shared library** for `/feature` and `/fix` lifecycle persistence:
  - SQLite-backed state machine: `active | paused | pending_audit | complete | budget_limited | failed | aborted`
  - Marker file at `~/.claude/state/.active-run` (O(1) Stop-hook stat-check)
  - Adversarial completion audit via `codex exec` subprocess (read-only sandbox, high reasoning effort)
  - Token budget tracking with `budget_limited` auto-transition
  - `~/.claude/bin/run-state` CLI: `start | status | update | audit | complete | abort | pause | resume | list`
- **`scripts/hooks/run-state-session.py`** — SessionStart hook anchors `CLAUDE_SESSION_ID` to `~/.claude/state/session.env`
- **`scripts/hooks/run-state-stop.py`** — Stop hook checks marker, blocks stop and injects continuation prompt if a run is active
- **`lib/run_state/prompts/{fix,feature}_audit.txt`** — hostile auditor prompts (bug-still-exists vs spec-not-complete)
- 28 tests in `lib/run_state/tests/` (state, marker, audit, CLI, stop hook). All green.

### Changed

- `skills/fix/SKILL.md` **v1.3.0 → v1.4.0**: added run-state lifecycle (start/update/audit/complete) + Step 4 adversarial audit + flags (`--tokens`, `--no-audit`)
- `skills/feature/SKILL.md` **v1.2.0 → v1.3.0**: same lifecycle + Step 6 spec-completion audit before `/canary` + 1.5M token default budget
- `setup.sh`: installs `lib/run_state/`, `bin/run-state`, `scripts/hooks/run-state-*.py`; registers Stop + SessionStart hooks in `~/.claude/settings.json`; checks for `python3`, `jq`, `codex` CLI prerequisites


## [Unreleased]

## 2026-05-08 — codex-gate cross-model adversarial review

### `feature` 1.1.0 → 1.2.0

**Added**
- New Step 5.5 `/codex-gate` — cross-model adversarial review on the final post-QA diff before `/ship`. Codex (OpenAI GPT-5) runs 3 passes: general review + adversarial + test-coverage gap analysis. Documented to catch CRITICAL bugs that pass clean through Claude-only quality gates.
- `--no-codex-gate` flag — opt out of Step 5.5. Default-on. Recommended only when blast radius is provably minimal.
- Skip-gracefully behavior — if `codex` CLI is not installed, Step 5.5 logs a WARNING with install instructions and continues to `/ship`. No hard-fail. Codex remains an optional dependency in README.
- New failure-handling row — codex-gate CRITICAL findings auto-stop the pipeline, write artifact `.ralph/feature-run-${SPEC_ID}-codex-critical.md`, exit 1. User fixes and reruns `/feature NNN --resume`.
- Hook integration — `scripts/hooks/codex-gate-warn.sh` (if present in user env) is updated by the gate run so subsequent `gh pr merge` does not warn about a missing recent codex-gate run.

**Changed**
- Cost estimate range bumped: ~$20-55 → ~$22-57 per feature (codex-gate adds ~$2 + ~13 min).
- Pipeline diagram and Related Skills section updated to reflect Step 5.5.

### `fix` 1.2.0 → 1.3.0

**Added**
- New Step 5.5 `/codex-gate` — full cross-model 3-pass review on the final post-QA diff. Step 3.5 still runs the lighter inline codex adversarial pass on the fresh fix; Step 5.5 runs the full gate against the diff including any retry-loop changes.
- `--no-codex-gate` flag — opt out of Step 5.5. Default-on.
- Skip-gracefully when `codex` CLI absent — WARNs and continues, mirroring Step 3.5 behavior.
- CRITICAL-finding artifact — `${RALPH_DIR}/codex-critical-findings.md` mirrors Step 3.5 abort path. Exit 1 with structured terminal error. No human prompt.
- Cost estimate notes — codex-gate adds ~$2 + ~13 min per fix.

**Changed**
- Final report block now includes a Codex-gate row.
- Integration table includes `/codex-gate` row.

### Documentation

- `README.md` — Codex CLI prerequisite section reframed: still optional but **strongly recommended** for production-blast-radius changes. Documents both the inline Step 3.5 use and the full gate (Step 5.5 in /feature and /fix). Adds `--no-codex-gate` to flags reference.
- `docs/commands.md` — adds `/codex-gate` row; updates pipeline order to include `/codex-gate` between `/review` and `/ship`.
- `docs/pipeline.md` — overview ASCII diagram now shows `/codex-gate` step between `/qa` and `/ship`.

**Why this matters**
Single-model review has documented blind spots. Cross-model adversarial review (Codex GPT-5 reading Claude-written diffs) has caught CRITICAL bugs that pass 3+ Claude-side quality gates clean — most notably compose `/bin/sh` masking shebang fix and trap-removing-extension-dir post-swap (caught by codex in v0.5.4.1 of the upstream openclaw repo). ~$2 + 13 min is cheap insurance vs hotfix-cycle days for any PR with production blast radius (auth, payments, RLS, multi-tenant, cron, infra scripts, disaster-recovery).

## 2026-04-29 — non-interactive harness + ruflo hardening

### `feature` 1.0.0 → 1.1.0

**Added**
- `--auto` flag (default) — wraps `/autoplan` in a non-interactive harness:
  - Phase 1 premise gate auto-approved
  - User Challenges gate auto-accepts the recommended option
  - Final approval gate auto-approves taste decisions
  - Tasks.md approval auto-approves after structural validation (model
    annotations, depends-on lines, task count 5–60, staging+prod phases)
- `--interactive` flag (escape hatch) — restores legacy v1.0.0 gate behavior:
  autoplan premise + taste decisions + tasks.md approval all prompt the user.
- `RUFLO_REQUIRED` env var — defaults to `1` (mandatory). Setting `0` is the
  documented debug-mode escape hatch and emits a WARNING on every spawn.
- New gate count summary at top of pipeline diagram:
  - Default `--auto`: 1 hard gate (prod promotion only — irreversible)
  - `--interactive`: 4 hard gates (autoplan premise, taste, tasks.md, prod)

**Removed**
- `--no-ruflo` flag. Ruflo is now mandatory. Use the `RUFLO_REQUIRED=0` env
  override for debugging-only fallback to native Agent.

**Changed**
- Step 2 (autoplan): instructs the assistant to auto-respond to autoplan
  AskUserQuestion gates by default (premise → A, User Challenges →
  recommended option, final gate → A). Logs every auto-decision to the run log
  with `auto_decision:` field.
- Step 3 (spec-decompose): replaces AskUserQuestion approval with structural
  validation in `--auto` mode. Aborts only when task count is out of bounds
  (<5 or >60).
- Step 4 (feature-implement): always invoked with `--ruflo`.
- Failure-handling table: adds rows for ruflo MCP unavailable and tasks.md
  validation out-of-bounds.

### `feature-implement` 1.0.0 → 1.1.0

**Changed**
- `USE_RUFLO=1` is now the default (previously `0`).
- Ruflo MCP pre-flight check before first task spawn. If `mcp__ruflo__*` is
  unreachable AND `RUFLO_REQUIRED=1` (default): hard-fail with structured
  error, log `event:"ruflo_hard_fail"` to `.implement-log.jsonl`, and exit 1.
  No silent fallback to native Agent.
- Native Agent fallback only when `RUFLO_REQUIRED=0` is explicitly set in env.
  Every native spawn emits a WARNING to stderr.
- Frontmatter `description` updated to reflect v1.1.0 ruflo-mandatory policy.

**Removed**
- Silent fallback path in Step 5 ruflo executor. The previous behavior — "On
  any `mcp__ruflo__*` failure, log the error and fall back to native Agent
  tool for the remaining tasks" — has been replaced with a hard-fail.

### `fix` 1.1.0 → 1.2.0

**Removed**
- `AskUserQuestion` removed from `allowed-tools` frontmatter list. The skill no
  longer prompts the user during a run.
- Step 1: AskUserQuestion fallback when investigate cannot determine root
  cause. Replaced with structured artifact (`investigation-incomplete.md`)
  and `exit 1`.
- Step 3.5: AskUserQuestion gate when CRITICAL findings surface from
  specialist or codex review. Replaced with structured artifact
  (`critical-findings.md`) and `exit 1`.

**Added**
- Step 3 ruflo pre-flight: hard-fails if MCP unavailable and
  `RUFLO_REQUIRED=1` (default). The previous `executor-detect.sh || echo
  "native"` silent fallback is gone.
- New artifact: `$RALPH_DIR/investigation-incomplete.md` — structured doc with
  fields: bug desc, attempts, what was explored, what's unclear, suggested
  narrow scope, resume command.
- New artifact: `$RALPH_DIR/critical-findings.md` — table of findings
  (severity, source, file:line, description) plus 3-option resolution path
  (rollback / override / follow-up patch).

**Changed**
- Frontmatter `description` updated to reflect non-interactive policy and
  v1.2.0 ruflo-mandatory policy.
- Safety rules section adds two new lines documenting the non-interactive
  policy and the ruflo policy.

### Migration notes

If you relied on the v1.0.0 `--no-ruflo` flag for `/feature` or
`/feature-implement`: set `RUFLO_REQUIRED=0` in your shell env before
invoking the skill. Expect WARNING lines on stderr — they are intentional
(degraded mode visible to the operator).

If you relied on the AskUserQuestion gates inside `/fix` (Step 1 fallback or
Step 3.5 CRITICAL gate): on the next abort, look in the run's `.ralph/fix-*`
directory for `investigation-incomplete.md` or `critical-findings.md`. The
artifacts are designed to be readable, actionable, and resumable without
needing the user to be present mid-run.

If you want the legacy `/feature` gate behavior back temporarily: invoke with
`/feature NNN --interactive`. This restores the autoplan gates and the
tasks.md approval gate exactly as v1.0.0 ran them.
