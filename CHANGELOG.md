# Changelog

All notable changes to feature-fix-swarm are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
on a per-skill basis. Each skill in `skills/` carries its own version field in
its SKILL.md frontmatter; this CHANGELOG aggregates user-facing changes across
all skills.

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
