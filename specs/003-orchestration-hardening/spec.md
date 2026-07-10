# Spec 003 — Orchestration hardening: prevention-first delegation, learnings harvest, risk-sized review, resilient autonomous runs

**Status:** draft · **Branch:** `003-orchestration-hardening` · **Date:** 2026-07-10
**Prior art:** `specs/003-orchestration-hardening/prior-art.md` (oh-my-claudecode 37.6k★, ruflo 63.7k★, agent-orchestrator 8.2k★ — pattern-mined 2026-07-10)

## Context

FFS v4.4.0 detects orchestration failures after the fact (delegation-audit is
post-hoc advisory), pays uniform review cost regardless of diff risk,
short-circuits fix rounds on the first blocking finding batch, discards gsd's
own learnings loop at run end, grades environments but never the agent harness
itself, and declares autonomous runs dead on single-signal probes. Eight
enhancements close these gaps. All are additive levers/skill-prose changes;
none touch the `gates.py` evidence exit-code path.

## User Stories

- **US1 (delegation-enforcer):** as an operator running FFS on a premium-tier
  host, I want unpinned sub-agent spawns to be blocked or auto-pinned at spawn
  time, so cost drift is prevented rather than reported next morning.
- **US2 (learnings harvest):** as an operator, I want each finished run's
  debugging lessons extracted and persisted automatically in the finish tail,
  so the next spec's planner benefits without me remembering a manual step.
- **US3 (review tiers):** as an operator, I want review-gate to size its
  pipeline to the diff's risk (files touched, security surface), so a 2-file
  docs diff doesn't cost the same as a 40-file auth change.
- **US4 (findings queue):** as an operator, I want every review pass to emit
  ALL findings with persisted per-finding signatures, so fix rounds work the
  whole queue and re-runs only re-litigate unresolved items.
- **US5 (harness audit):** as an operator, I want preflight to score the agent
  harness itself (skill-link drift, vendored-copy staleness, dead model pins),
  so an unattended run never starts on a silently broken harness.
- **US6 (deslop gate):** as an operator, I want an optional lightweight
  deletion-first cleanup pass scoped to just-changed files before review-gate,
  so reviewers see a smaller, slop-free diff.
- **US7 (fix→gsd-debug):** as an operator, I want `/fix` to route non-obvious
  reproductions through gsd's scientific-method debug loop, so hard bugs get
  hypothesis-driven investigation instead of ad-hoc probing.
- **US8 (termination guardrail):** as an operator, I want autonomous-run
  liveness decided by AND-of-all-signals (process + recent activity + owned
  in-flight work), so one transient failed probe never kills an overnight wave.

## BDD Scenarios

Feature: prevention-first delegation and resilient autonomous orchestration

Scenario: unpinned spawn gets auto-pinned (US1 happy path)
  Given a PreToolUse delegation-enforcer hook is registered and `.planning/config.json` carries model overrides
  When the orchestrator dispatches an Agent call without a `model` parameter
  Then the hook injects the pin resolved from config and the spawn proceeds at the pinned tier

Scenario: enforcer stays out of the way when pin present (US1)
  Given the delegation-enforcer hook is registered
  When the orchestrator dispatches an Agent call that already carries `model: haiku`
  Then the call passes through unmodified

Scenario: enforcer fails open without config (US1 error path)
  Given no `.planning/config.json` exists in the repo
  When an unpinned Agent call is dispatched
  Then the hook warns on stderr and allows the call unchanged

Scenario: learnings harvested at finish (US2 happy path)
  Given a gsd run completed a phase and `learnings.jsonl` contains entries
  When the finish tail runs its learnings step
  Then the distilled learnings are persisted to the configured memory backend and the report lists the count

Scenario: learnings step never blocks ship (US2 error path)
  Given the memory backend is unreachable
  When the finish tail runs its learnings step
  Then the step records a warning and the finish tail continues to review-gate

Scenario: small safe diff gets light review (US3 happy path)
  Given a staged diff touching 3 files with no security-surface paths
  When review-gate runs with tier auto-detection
  Then only the single-pass light review executes and the gate reports its selected tier

Scenario: security-surface diff forces full tier (US3)
  Given a staged diff touching 2 files, one matching the security-surface pattern list
  When review-gate runs with tier auto-detection
  Then the full 3-pass pipeline plus cross-vendor adversary executes regardless of diff size

Scenario: operator overrides tier (US3)
  Given `REVIEW_TIER=full` is exported
  When review-gate runs on a 2-file docs diff
  Then the full pipeline executes and the report notes the manual override

Scenario: all findings queued in one pass (US4 happy path)
  Given a review pass produced 3 HIGH findings across different files
  When the gate records its verdict
  Then all 3 findings are persisted with unique signatures and the fix round receives the complete queue

Scenario: resolved findings not re-litigated (US4)
  Given a findings queue where 2 of 3 signatures are marked resolved
  When review-gate re-runs
  Then only the unresolved finding is re-checked and the report shows 2 deduped

Scenario: harness audit degrades preflight (US5 happy path)
  Given a skill symlink in `~/.claude/skills` points at a missing target
  When preflight runs with the harness-audit section enabled
  Then the audit reports the dangling link with a reduced score and preflight surfaces it as an advisory finding

Scenario: healthy harness scores clean (US5)
  Given all installed skills resolve and vendored copies match their source shas
  When the harness audit runs
  Then it reports a passing score and preflight proceeds silently

Scenario: deslop pass shrinks the diff (US6 happy path)
  Given an implementation diff containing dead branches and duplicated helper code, with a green test suite
  When the operator runs the slop-only uplift pass against the diff base
  Then the pass deletes the slop, the test suite still passes, and the resulting diff is strictly smaller

Scenario: deslop refuses without green baseline (US6 error path)
  Given the test suite is failing before the pass starts
  When the operator runs the slop-only uplift pass
  Then the pass refuses to edit and reports the failing baseline

Scenario: hard bug routes to debug loop (US7 happy path)
  Given `/fix` is invoked for a bug whose reproduction is not a single obvious command
  When the fix workflow reaches its diagnose step
  Then it delegates to the gsd debug loop and consumes its root-cause report before editing

Scenario: transient probe does not kill run (US8 happy path)
  Given an autonomous run whose worker process is alive and produced activity within the freshness window
  When one liveness probe fails transiently
  Then the run is NOT declared dead and the composite check reports which signals held

Scenario: truly dead run is adoptable (US8)
  Given a worker process that is gone AND has no activity within the window AND owns no in-flight ship action
  When the composite liveness check runs
  Then the run is declared abandoned and adopt-wip may proceed

## Acceptance Criteria

- AC-001: `scripts/hooks/delegation-enforcer.sh` exists, registered as PreToolUse guidance in setup/docs; given Agent/Task tool input JSON on stdin without `model`, it emits the same JSON with `model` injected from `.planning/config.json` resolution; with `model` present, byte-identical passthrough; without config, passthrough + stderr warn. Exit 0 in all non-usage cases (advisory injection, never blocks).
- AC-002: kill-switch `DELEGATION_ENFORCER=off` yields passthrough.
- AC-003: feature-implement + code-uplift finish tails contain a learnings step invoking `scripts/gsd/learnings-harvest.sh`, which reads `.planning/**/learnings*.jsonl` (and gsd extract output when present), writes distilled entries to gbrain when available, else appends to `.feature-fix-swarm/learnings-archive.jsonl`; fail-soft (exit 0 always), reports count.
- AC-004: review-gate SKILL.md carries a tier-selection preamble: LIGHT (<5 files, no security surface, <200 changed lines) = single general pass; STANDARD = current 3-pass; FULL (>20 files OR any security-surface path OR migration) = 3-pass + adversary at `xhigh`. Security-surface pattern list is sourced from the same list as `security-model-fence.sh` (one home).
- AC-005: `REVIEW_TIER={light|standard|full}` env overrides auto-detection; selected tier + reason printed in the gate header.
- AC-006: `gates.py findings-queue` subcommand: `add` (finding → stable signature = sha256 of file+normalized-issue), `list --unresolved`, `resolve <sig>`; store in the existing evidence store JSON under a `findings` key; unit-tested.
- AC-007: review-gate records all pass findings via findings-queue before verdict; re-run consults queue and skips resolved signatures, reporting `deduped: N`.
- AC-008: `scripts/harness-audit.py` scores 0-100 across: dangling skill links, vendored-copy drift (skill version vs packaged version), dead model pins in `.planning/config.json`, unregistered-hook drift; `--json` output; preflight skill text gains an advisory harness-audit section invoking it; never blocks preflight (advisory).
- AC-009: `code-uplift` gains `--slop-only <diff-base>` fast path: green-baseline wall (refuse on failing suite), deletion-first scope limited to files in the diff, re-runs suite after, reports net line delta. Documented as optional finish-tail step 0.
- AC-010: `fix` SKILL.md diagnose step routes to `/gsd-debug` when reproduction is non-obvious (skill-prose contract with explicit criteria: no failing test yet AND no single-command repro).
- AC-011: `scripts/gsd/liveness-check.sh`: composite AND-of-signals (pid alive; newest mtime under run state/transcript within `LIVENESS_WINDOW_MIN` default 30; no in-flight granted ship action) → exit 0 alive / 1 dead, prints per-signal verdicts; adopt-wip skill text consults it before declaring WIP abandoned.
- AC-012: every new/changed script is shellcheck-clean (bash) or pytest-covered (python); bats coverage for each new script's happy + error paths; existing suites stay green (baseline: pytest 231, bats 64).
- AC-013: CHANGELOG.md v4.5.0 entry + README skill/script counts updated; `setup.sh` installs all new scripts (asserted by `tests/bats/setup-install.bats` extension).

## E2E Test Paths

E2E in this repo = bats-driven CLI journeys (no browser surface; Playwright not applicable — see plan.md Test Strategy note).

- PATH-001: unpinned Agent-call JSON piped through delegation-enforcer with seeded config → pinned JSON out; pinned JSON → byte-identical; no config → passthrough + warn; `DELEGATION_ENFORCER=off` → passthrough.
- PATH-002: seeded fake `learnings.jsonl` + stubbed gbrain → harvest run → archive/backend receives entries, exit 0; unreachable backend → warn + exit 0.
- PATH-003: fixture diffs (3-file docs / 2-file auth-touching / 25-file) through tier-detection helper → LIGHT / FULL / FULL selected; `REVIEW_TIER` override honored.
- PATH-004: findings-queue lifecycle — add 3, list 3 unresolved, resolve 1, list 2, re-add duplicate signature → deduped.
- PATH-005: harness-audit against a fixture harness dir with one dangling link + one stale vendored version → score < 100, both findings named in `--json`; clean fixture → 100.
- PATH-006: slop-only uplift against fixture repo with failing suite → refusal; green suite → runs and reports delta.
- PATH-007: liveness-check matrix — alive pid + fresh mtime → exit 0; dead pid + stale mtime + no ship grant → exit 1; dead pid + fresh mtime → exit 0 (declare dead ONLY when ALL signals dead — exact truth table in plan.md).

## Edge Cases

- EDGE-001: enforcer receives non-Agent tool JSON (e.g. Bash) → byte-identical passthrough, no parse attempt beyond tool-name check.
- EDGE-002: `.planning/config.json` exists but has no model overrides key → passthrough + warn (treat as no-config).
- EDGE-003: review-tier diff base is orphan/unresolvable sha → `standard` fail-safe, never `light` (under-review beats over-trust).
- EDGE-004: findings-queue signature collision across DIFFERENT files impossible by construction (file path in hash input); same file + reworded issue → distinct signatures accepted (dedup is exact-normalized, not fuzzy — documented limitation).
- EDGE-005: liveness pid file contains PID reused by an unrelated process → mtime + grant signals still counted; accepted false-alive risk documented (window-bounded).
- EDGE-006: learnings JSONL contains malformed lines → skip line, count skipped, never abort harvest.
- EDGE-007: harness-audit on machine without `~/.claude/skills` → score 100 with `skipped: no harness dir` note (absence ≠ drift).
- EDGE-008: `--slop-only` diff base equals HEAD (empty diff) → no-op success, `0 files in scope`.

## E2E Playwright Stubs

Not applicable — no browser surface in this repo (CLI skill suite). Per-PATH
bats journey stubs replace Playwright (Test Strategy note, plan.md):

```bash
# tests/bats/e2e-path-001.bats (representative stub shape; PATH-002..007 same pattern)
@test "PATH-001: unpinned spawn JSON gets pinned from seeded config" {
  seed_planning_config_with_overrides            # arrange
  run bash scripts/hooks/delegation-enforcer.sh <<<"$UNPINNED_AGENT_JSON"   # act
  [ "$status" -eq 0 ]                            # assert
  echo "$output" | jq -e '.tool_input.model == "sonnet"'
}
```

## Test Contract Summary

| Layer | Count | Status |
|---|---|---|
| BDD Scenarios | 17 | draft |
| Unit test cases | 22 | listed |
| Unit test files | 8 | mapped |
| Integration tests | 6 | defined |
| E2E paths (bats journeys) | 7 | stubbed |

## Out of scope

- Tournament/arena multi-candidate selection, CDC/SSE event bus, multi-CLI adapters beyond claude/codex, ETag polling (LOW tier — separate micro-PR if wanted), persistent reviewer sessions (claude-side SendMessage continuation — deferred, needs harness-level support verification).
- Any change to `gates.py verify_done`/`run_gate` exit-code semantics.
