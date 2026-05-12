# Changelog

All notable changes to feature-fix-swarm are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
on a per-skill basis. Each skill in `skills/` carries its own version field in
its SKILL.md frontmatter; this CHANGELOG aggregates user-facing changes across
all skills.

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
