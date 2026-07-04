# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
on a per-skill basis. Each skill in `skills/` carries its own version field in
its SKILL.md frontmatter; this CHANGELOG aggregates user-facing changes across
all skills.

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
