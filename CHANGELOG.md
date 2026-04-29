# Changelog

All notable changes to feature-fix-swarm are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
on a per-skill basis. Each skill in `skills/` carries its own version field in
its SKILL.md frontmatter; this CHANGELOG aggregates user-facing changes across
all skills.

## [Unreleased]

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
