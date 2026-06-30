# Feature Fix Swarm Commands Reference

Quick reference for all available commands in the feature-fix-swarm harness across Claude Code and Codex runtimes.

## Feature Development Pipeline

| Command | What it does |
|---------|-------------|
| `/office-hours` | Brainstorm product ideas, validate "is this worth building", structured problem statement |
| `/feature-spec NNN` | **Spec-first pipeline:** speckit.specify → speckit.plan → speckit.clarify, each phase enforcing TDD unit test list, BDD Given/When/Then scenarios, and E2E Playwright stubs. Run before `/autoplan`. |
| `/autoplan` | Full review pipeline: CEO + Eng + DX dual voices with Codex, auto-decides taste decisions; `--accept-all-recommendations` auto-selects every recommended answer |
| `/spec-decompose NNN` | Turn `specs/NNN/plan.md` into normalized `tasks.md` with host-aware `[model:]` `[agent:]` `[qa:]` annotations |
| `/feature-implement NNN` | Execute tasks.md one-by-one via sub-agents. `--qa-loop` (default ON), `--dry-run`, `--one`, `--qa-openclaw`, `--qa-telegram` |
| `/feature NNN` | End-to-end: bootstrap spec if needed, autoplan, decompose, implement, qa, ship, canary. 2 hard gates. `--accept`, `--accept-all-recommendations`, `--goal`, `--qa-openclaw`, `--qa-telegram` |

**Pipeline order:** `/office-hours` → `/feature-spec NNN` (TDD+BDD+E2E contracts) → `/autoplan` → `/spec-decompose` → `/feature-implement` → `/qa` → `/review` → `/review-gate` → `/ship` → `/land-and-deploy` → `/canary`

### /feature-spec flags

| Flag | Effect |
|------|--------|
| (none) | Full pipeline: speckit.specify → speckit.plan → speckit.clarify |
| `--no-clarify` | Stop after speckit.plan; skip clarify phase |
| `--dry-run` | Preview what would be generated without writing files |

## QA + Testing

| Command | What it does |
|---------|-------------|
| `/qa` | Full browser-based QA via `$B` (gstack browse). Finds bugs, takes screenshots, reports. |
| `/qa-only` | Report-only QA. Tests but never fixes. Good for "just tell me what's broken." |
| `/investigate` | Systematic root cause analysis (5 Whys). Scope-locks to affected module. |
| `/tdd` | TDD workflow: write test first (RED), implement (GREEN), refactor — one atomic behavior per cycle |

## Code Review + Quality

| Command | What it does |
|---------|-------------|
| `/review` | Pre-landing code review: checklist pass + specialist army + adversarial review |
| `/plan-ceo-review` | Strategy review of a plan file (premises, scope, alternatives) |
| `/plan-eng-review` | Architecture review (coupling, test gaps, performance, security) |
| `/plan-design-review` | UI/UX review (7 dimensions, interaction states, responsive) |
| `/plan-devex-review` | Developer experience review (TTHW, error messages, CLI ergonomics) |
| `/codex` | Run OpenAI Codex as outside voice for adversarial second opinion |
| `/review-gate` | Cross-model 3-pass review (general + adversarial + test-coverage gap) on the staged diff. Canonical gate used by `/feature` Step 5.5 and `/fix` Step 5.5 before /ship. `/codex-gate` remains a compatibility alias. ~$2 + ~13 min. |

## Ship + Deploy

| Command | What it does |
|---------|-------------|
| `/ship` | Full automated ship: tests, review, version bump, CHANGELOG, commit, push, PR |
| `/land-and-deploy` | Verify production health after merge (curl checks on all Vercel sites) |
| `/canary` | Monitor prod for 1h post-deploy, auto-rollback if error rate > 1% |
| `/document-release` | Sync all docs (README, CLAUDE.md, TODOS.md) with shipped changes |

## Design + Brand

| Command | What it does |
|---------|-------------|
| `/design-consultation` | Design system decisions, brand alignment |
| `/design-review` | Full visual audit of UI changes |
| `/design-shotgun` | Rapid design iteration with multiple variants |

## Bug Fix (Ralph Loop)

| Command | What it does |
|---------|-------------|
| `/fix "bug description"` | Full loop: investigate (5 Whys) then fix (ruflo agents) then qa-only then full qa. Loops until green. |
| `/fix "desc" --plan` | Use /plan-eng-review for complex bugs needing architectural review |
| `/fix "desc" --no-qa` | Skip full /qa, only run /qa-only on affected area |
| `/fix "desc" --dry-run` | Investigate + plan but don't apply the fix |
| `/fix "desc" --scope=file1,file2` | Manually scope-lock to specific files |

## Debugging

| Command | What it does |
|---------|-------------|
| `/investigate` | 5 Whys root cause analysis, scope-locked to affected module |
| `/browse` | Open `$B` (gstack browser) for manual inspection |

## Project Management

| Command | What it does |
|---------|-------------|
| `/retro` | Weekly retrospective of what shipped, what broke, what to improve |
| `/checkpoint` | Save progress mid-session for resume later |
| `/health` | Codebase quality check (dead code, test coverage, lint) |

## QA Ralph Loop Flags

These flags work with `/feature-implement`:

| Flag | Effect |
|------|--------|
| `--qa-loop` | Enable per-phase QA (default ON). 2 test hooks + 3 LLM agents per phase. |
| `--no-qa-loop` | Disable the Ralph loop entirely |
| `--qa-skip e2e,security` | Skip specific QA dimensions at runtime |
| `--qa-only review` | Run only specified QA dimensions |
| `--dry-run` | Print the execution plan without spawning agents |
| `--resume` | Pick up from last failure point |
| `--ruflo` | Use ruflo swarm executor instead of native Agent |
| `--one` | Execute only the next unchecked task |

## Environment Variables

| Var | Default | Effect |
|-----|---------|--------|
| `RALPH_MAX_RETRIES` | `3` | Max retry attempts per phase on QA failure |
| `RALPH_AUTO_QA` | `1` | Set to `0` to disable PostToolUse auto-qa hook |
| `RALPH_EXECUTOR` | (auto) | Force `ruflo` or `native` executor |
| `RALPH_DEBOUNCE_SECS` | `30` | Quiet window before auto-qa fires |

## Ruflo (Intelligent Orchestration)

```bash
npx ruflo@latest progress summary       # What was done this session
npx ruflo@latest memory search "query"   # Search what was learned
```

| Scenario | Use |
|----------|-----|
| Spawning 3+ independent sub-agents | `mcp__ruflo__swarm_init` then `mcp__ruflo__agent_spawn` |
| Check if similar task was done before | `mcp__ruflo__agentdb_pattern-search` |
| Route task to right model tier | `mcp__ruflo__hooks_model-route` |
| Store a reusable pattern | `mcp__ruflo__agentdb_pattern-store` |

Ruflo routing is most reliable when task `agent:` values are normalized to a small canonical role set before spawning: `coordinator`, `architect`, `researcher`, `coder`, `tester`, `reviewer`.

## Reference

- [TDD & BDD Guide](tdd-bdd-guide.md) — Research-backed best practices (Fowler + MSR 2026), anti-patterns, agent over-mocking warning, Gherkin rules, test pyramid
- [Pipeline overview](pipeline.md) — Full pipeline diagram with QA Ralph loop
- [QA Ralph Loop](qa-ralph-loop.md) — Per-phase QA architecture and configuration
- [Master context](../master-context.md) — Single-file reference for all integrated systems
