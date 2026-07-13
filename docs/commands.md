# Feature Fix Swarm Commands Reference

Quick reference for all available commands in the feature-fix-swarm harness across Claude Code and Codex runtimes.

## Feature Development Pipeline

| Command | What it does |
|---------|-------------|
| `/office-hours` | Brainstorm product ideas, validate "is this worth building", structured problem statement |
| `/feature-spec NNN` | **Spec-first pipeline:** speckit.specify → speckit.plan → speckit.clarify, each phase enforcing TDD unit test list, BDD Given/When/Then scenarios, and E2E Playwright stubs. Run before `/autoplan`. |
| `/autoplan` | Full review pipeline: CEO + Eng + DX dual voices with Codex, auto-decides taste decisions; `--accept-all-recommendations` auto-selects every recommended answer |
| `/spec-decompose NNN` | Turn `specs/NNN/plan.md` into normalized `tasks.md` with host-aware `[model:]` `[agent:]` `[qa:]` annotations usable on Claude or Codex |
| `/plan-decompose "description"` | Turn a description or existing plan into `tasks.md` via autonomous eng review + `/review-gate` — no speckit interview. Faster path when a spec isn't warranted. |
| `/feature-implement NNN` | Execute tasks.md one-by-one via sub-agents. `--qa-loop` (default ON), `--dry-run`, `--one`, `--qa-openclaw`, `--qa-telegram` |
| `/feature NNN` | End-to-end: bootstrap spec if needed, autoplan, decompose, implement, qa, ship, canary. 2 hard gates. `--accept`, `--accept-all-recommendations`, `--goal`, `--qa-openclaw`, `--qa-telegram` |
| `/swarm "task description"` | Ad-hoc task swarm — no spec dir required. Classifies the task (model/agent/thinking tier) and executes via the gsd-core loop (see `/gsd-*` commands). |

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
| `/review-gate` | Cross-model 3-pass review (general + adversarial + test-coverage gap) on the staged diff. Canonical gate used by `/feature` Step 5.5 and `/fix` Step 5.5 before /ship. `/review-gate` remains a compatibility alias. ~$2 + ~13 min. |

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
| `/fix "bug description"` | Full loop: investigate (5 Whys) then fix (gsd executors) then qa-only then full qa. Loops until green. |
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
| `/goal-wrap [--gates] "objective"` | Bundle current work into a self-contained, anti-drift `/goal "..."` prompt with tracked DONE WHEN proof commands. Use before `/clear`, agent handoff, or switching machines. `--gates` reverts to ask-first behavior (default: full autonomy — commits/push/merge/deploy pre-approved). Degrades gracefully without repowise/gbrain/`/prompt-master`/`/handoff` — see the skill's own "Soft dependencies" table. |

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
| `--one` | Execute only the next unchecked task |

## Environment Variables

| Var | Default | Effect |
|-----|---------|--------|
| `RALPH_MAX_RETRIES` | `3` | Max retry attempts per phase on QA failure |
| `RALPH_AUTO_QA` | `1` | Set to `0` to disable PostToolUse auto-qa hook |
| `RALPH_EXECUTOR` | (auto) | Legacy; execution now flows through the gsd loop |
| `RALPH_DEBOUNCE_SECS` | `30` | Quiet window before auto-qa fires |

## gsd-core (Orchestration)

The gsd loop replaced ruflo as FFS's orchestrator (spec 002). Pin: `@opengsd/gsd-core@1.6.1`.
Use `node node_modules/.bin/gsd-tools` — bare `npx gsd` resolves to the WRONG package.

| Scenario | Use |
|----------|-----|
| Seed a project from a spec | `/spec-decompose NNN` (writes `.planning/`, drives `/gsd-plan-phase`) |
| Execute the current phase | `/feature-implement NNN` → `/gsd-execute-phase N` |
| Unattended run | `/feature-implement NNN --autonomous` (preflight PASS + grant ledger required) |
| Headless drive | `TIMEOUT=3600 bash scripts/gsd/gsd-run.sh /gsd-<cmd> ...` (trimmed-MCP, auth-scrubbed) |
| Quick single-task fix | `/fix` → `/gsd-quick` |
| Resume after context reset | `/gsd-resume-work` (STATE.md is the resume point) |
| Verifier found gaps | `/gsd-plan-phase N --gaps` → `/gsd-execute-phase N --gaps-only` |

Before phase execution, `scripts/gsd/requirement-ownership-gate.sh N` requires
the phase's PLAN frontmatter to own every ROADMAP requirement exactly once.
Preparatory plans use `requirements: []`; an ID goes only on the final plan that
genuinely completes it. Headless execution enforces this before any model probe.

Completion authority is gates.py, never gsd self-report: `workflow.test_command` =
`scripts/gsd/gates-test-command.sh` (run-gate + strict verify-done),
`workflow.code_review_command` = `scripts/gsd/review-gate-command.sh` (grant wall + codex),
and the `gsd-phase-evidence-gate.sh` PreToolUse hook blocks ROADMAP/STATE phase flips
without evidence.

Do not use provider-key execution paths for feature tasks. Codex sessions execute
through `codex exec`; Claude sessions execute through `claude -p`.

## Reference

- [TDD & BDD Guide](tdd-bdd-guide.md) — Research-backed best practices (Fowler + MSR 2026), anti-patterns, agent over-mocking warning, Gherkin rules, test pyramid
- [Pipeline overview](pipeline.md) — Full pipeline diagram with QA Ralph loop
- [QA Ralph Loop](qa-ralph-loop.md) — Per-phase QA architecture and configuration
- [Master context](../master-context.md) — Single-file reference for all integrated systems

## Machine gates (lib/gates.py)

Completion authority for autonomous runs — evidence, not agent self-report.

| Command | What it does |
|---------|-------------|
| `gates.py record-gate T042 --exit N --cmd ... --before ... --after ...` | Record a task's test-gate outcome in the evidence store (`$GATES_STORE`, default `.feature-fix-swarm/evidence.json`) |
| `gates.py verify-done T042` | Exit 0 iff passing gate evidence exists — the ONLY thing that legalizes an `[X]` flip. Prints `executed_by` |
| `gates.py verify-done T042 --strict` | (or `GATES_STRICT=1`) additionally reject caller-recorded evidence — only `run-gate` runner evidence passes. The loop runs strict (v3.14.0) |
| `gates.py run-gate T042 -- CMD…` | PREFERRED: execute the gate and record the REAL exit code — evidence bound to the runner |
| `gates.py run-red T041 -- CMD…` | PREFERRED: execute the RED test; proof stored only if it really failed |
| `gates.py record-red T041 --exit N < log` | Store a RED proof; rejected unless the log shows a real failure. Prints a forgeability WARNING |
| `gates.py check-red T041` | Exit 0 iff RED proven — blocks the paired GREEN task until then |
| `gates.py phase-score T040 T041 …` | Truth score from stored evidence (compile .35 / tests .25 / lint .20 / typecheck .20, normalized over categories present; missing evidence → 0.0). Exit 1 below 0.95 → phase rollback (v3.14.0) |
| `gates.py note-failure T042 --sig "…"` | Record a failure signature; exit 1 when the same signature repeats twice in a row → STOP, no-progress (v3.14.0) |
| `gates.py proof RUN T040 T041 … [--defer "name: reason"]… [--strict] [--out path]` | Emit the per-run proof artifact (`proof-<run>.json`): one claim per task with evidence cmd, real exit, sha256 of stored log material, live-vs-structural kind; named deferrals; go/no-go verdict. Exit 1 on no-go (v3.15.0) |
| `gates.py scan-tamper < diff` | Flag reward-hacking moves: deleted asserts, added skips, `exit 0`, CI edits. Exit 1 on findings |
| `gates.py analyze spec.md tasks.md` | Spec↔tasks coherence gate (spec-kit analyze analog); `/feature-implement` refuses to start on findings |

Gate ladder (cheap→expensive, fail-fast): compile → typecheck → lint → unit → integration → e2e → LLM review. Truth score is computed by `phase-score` at every phase gate; < 0.95 after max retries → rollback to phase checkpoint. No-progress is enforced by `note-failure` in the retry loop. LLM review rounds capped at 2/phase. `record-gate` still exists for humans but warns at runtime and is rejected under strict mode. Optional PreToolUse hook `hooks/tdd-gate.sh` blocks source writes with no matching test file.
