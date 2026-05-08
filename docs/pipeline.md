# Agent Harness Pipeline

End-to-end flow from feature idea to production, using gstack + spec-kit + ruflo.

**Last updated:** 2026-04-16
**Maintainer:** ralph contributors
**Kill criteria:** see [kill-criteria.md](kill-criteria.md) (TODO)

## Overview

```
┌────────────────────────────────────────────────────────────────────────┐
│                                                                        │
│   IDEA                                                                 │
│    │                                                                   │
│    ▼                                                                   │
│   /office-hours  ──►  ~/.gstack/.../design-*.md   (why to build?)      │
│    │                                                                   │
│    ▼                                                                   │
│   Write specs/NNN-name/plan.md manually                                │
│    (OR use /speckit.plan if you want scaffolding)                      │
│    Optional: specs/NNN-name/spec.md for user stories                   │
│    │                                                                   │
│    ▼                                                                   │
│   /autoplan  ──►  reviews plan.md in place                             │
│    │              dual voices + Decision Audit Trail                   │
│    ▼                                                                   │
│   /spec-decompose  ──►  specs/NNN-name/tasks.md                        │
│    │                    (Sonnet + prompts/decompose-spec.md)           │
│    │                    custom format: [model:] [agent:] [US] [P]      │
│    │                    QA tiers: unit/int/E2E + dev/staging/prod      │
│    │                    spec.md OPTIONAL — falls back to plan.md       │
│    ▼                                                                   │
│   [post-spec-write.sh hook — not yet wired, script ready]              │
│    │   ├─ linear-auto-sync (child issues)                              │
│    │   ├─ post-task-complete (detect [X] transitions)                  │
│    │   └─ ruflo-load (future — loads tasks into ruflo swarm)           │
│    │                                                                   │
│    ▼                                                                   │
│   /feature-implement [NNN]                                             │
│    │   iterates tasks.md; spawns Agent per [model:] annotation         │
│    │   sequential execution, updates [ ] → [X] or [F] on completion    │
│    │   --dry-run, --all, --task T042 flags supported                   │
│    │                                                                   │
│    ▼                                                                   │
│   /qa     (built INTO tasks.md phases — Dev QA per story)              │
│   /codex-gate ──►  cross-model 3-pass review (default-on)              │
│                    skip with --no-codex-gate                           │
│   /ship   ──►  PR + staging deploy                                     │
│   /canary ──►  production monitor + rollback                           │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

## Key simplification (2026-04-16)

**You do NOT need `/speckit.specify` or `/speckit.plan`** in the gstack-driven flow.

- `/office-hours` produces the problem framing
- You write `specs/NNN-feature-name/plan.md` directly (or use `/speckit.plan` as a scaffold if you prefer)
- `spec.md` is **optional** — `/spec-decompose` extracts user stories from plan.md or treats the whole feature as a single story when missing
- `/autoplan` reviews the plan (works on any file path)
- `/spec-decompose` produces tasks.md
- `/feature-implement` executes tasks one at a time with per-task model routing

## The user gates

At each gate, the user reviews the generated artifact and approves or iterates:

| Gate | After | Artifact | Decision |
|------|-------|----------|----------|
| 1 | /office-hours | design doc | premise + wedge correct? |
| 2 | write plan.md (manual or /speckit.plan) | plan.md | tech approach sound? |
| 3 | /autoplan | reviewed plan.md | accept challenges + taste decisions? |
| 4 | /spec-decompose | tasks.md | decomposition atomic + deps right? |
| 5 | /feature-implement per task or --all | tasks done | tests pass, behavior verified? |
| 6 | staging deploy | live URL | promote to prod? |

**Optional `/speckit.specify`**: if you want user stories in a separate spec.md, run it after /office-hours. /spec-decompose will use it. If you skip it, /spec-decompose falls back to plan.md or treats the feature as a single user story.

## Commands quick reference

### Planning layer

| Command | Purpose | Output |
|---------|---------|--------|
| `/office-hours` | Problem framing, premise challenge, Codex steelman | `~/.gstack/projects/$SLUG/{user}-{branch}-design-{dt}.md` |
| `/speckit.specify "X"` | Generate spec.md from natural language; creates branch | `specs/NNN-feature-name/spec.md` + git branch |
| `/speckit.clarify` | Q&A to resolve spec ambiguities | mutates spec.md |
| `/speckit.plan "tech stack"` | Generate plan.md + research.md + contracts | `specs/NNN/plan.md` + support files |
| `/autoplan` | Dual-voice review (CEO + Eng + DX + optional Design) | mutates plan.md with Decision Audit Trail |
| `/speckit.analyze` | Cross-artifact consistency check | report only |
| `/spec-decompose [NNN]` | Generate tasks.md with Sonnet via canonical prompt | `specs/NNN/tasks.md` |

### Execution layer

| Command | Purpose | Status |
|---------|---------|--------|
| `/feature [NNN]` | **End-to-end pipeline**: autoplan → decompose → implement → qa → ship → canary. 2 gates. Default. | available |
| `/feature [NNN] --resume` | Resume after any failure | available |
| `/feature [NNN] --no-ruflo` | Use native Agent tool instead of ruflo swarm | available |
| `/feature-implement [NNN]` | **Default: run ALL tasks**. Per-task model routing via `[model:]` annotation | available |
| `/feature-implement [NNN] --one` | Execute only the next unchecked task | available |
| `/feature-implement [NNN] --ruflo` | Use ruflo swarm executor (parallel [P] groups, falls back to Agent on error) | available |
| `/feature-implement [NNN] --dry-run` | Print next task without spawning | available |
| `/speckit.implement` | Upstream spec-kit executor (generic, ignores annotations) | available |

### QA + Ship layer

| Command | Purpose |
|---------|---------|
| `/qa` | Browser-based UX testing, bug finding, auto-fix |
| `/investigate` | Root-cause debugging (4-phase) |
| `/review` | Pre-landing diff review |
| `/cso` | Security audit (OWASP + STRIDE) |
| `/ship` | Tests + CHANGELOG + PR creation + merge-to-staging |
| `/land-and-deploy` | Merge to main + production verification |
| `/canary` | Post-deploy monitoring with screenshot baselines |

## Artifacts and locations

| Artifact | Location | Produced by |
|----------|----------|-------------|
| Design doc | `~/.gstack/projects/$SLUG/*-design-*.md` | `/office-hours` |
| CEO plan | `~/.gstack/projects/$SLUG/ceo-plans/*.md` | `/plan-ceo-review` |
| Eng test plan | `~/.gstack/projects/$SLUG/*-eng-review-test-plan-*.md` | `/plan-eng-review` |
| Autoplan restore | `~/.gstack/projects/$SLUG/*-autoplan-restore-*.md` | `/autoplan` |
| Spec | `specs/NNN-feature-name/spec.md` | `/speckit.specify` |
| Plan | `specs/NNN-feature-name/plan.md` | `/speckit.plan` |
| Research | `specs/NNN-feature-name/research.md` | `/speckit.plan` |
| Data model | `specs/NNN-feature-name/data-model.md` | `/speckit.plan` |
| Contracts | `specs/NNN-feature-name/contracts/*` | `/speckit.plan` |
| Tasks | `specs/NNN-feature-name/tasks.md` | `/spec-decompose` |
| Harness state | `specs/NNN/.harness-state.json` | `scripts/hooks/post-spec-write.sh` |
| Harness log | `specs/NNN/.harness.log` | `scripts/hooks/post-spec-write.sh` |
| Eval baseline | `~/.gstack/projects/$SLUG/harness-eval-*.json` | `scripts/harness-eval.sh` |

## Custom tasks.md format

Every task line:
```
- [ ] T### [P?] [US?] [model:X thinking:Y] [agent:dept/role] Description with `file/path.ext`
      Depends-on: T005, T012
```

**Fields:**
- `[ ]` checkbox → `[X]` done → `[F]` failed → `[S]` skipped
- `T###` sequential zero-padded ID
- `[P]` parallel-safe (optional)
- `[USn]` user-story tag (required in story phases, forbidden in Setup/Integration)
- `[model:X thinking:Y]` tier for implementing agent — `haiku/low`, `sonnet/med` (default), `sonnet/high`, `opus/max`
- `[agent:dept/role]` routing hint for ruflo or sub-agent delegation
- Description ends with backticked path relative to repo root
- `Depends-on:` line (indented 6 spaces) for task prerequisites

**Phase structure:**
1. Setup (boilerplate, migrations, deps)
2. Foundational (blocking prereqs for all stories)
3+. One phase per user story (Tests RED → Implementation GREEN → Dev QA)
N+1. Cross-story Integration (E2E, dev env)
N+2. Staging Deploy & Soak (sign-off gate, 24h soak)
N+3. Production Promotion (merge → canary → verify)
N+4. Rollback Plan

## QA tiers (in tasks.md)

| Tier | Environment | Tests | Responsibility |
|------|-------------|-------|----------------|
| Unit | localhost | vitest / pytest | implementer |
| Integration | localhost | vitest with real DB | implementer |
| E2E | localhost | Playwright | qa-engineer agent |
| Dev QA | localhost | manual dogfood per story | qa-engineer agent |
| Staging | live staging URL | smoke + 24h soak | qa-engineer agent |
| Production | live prod URL | canary + 1h monitor | devops-engineer agent |

## Eval and baseline

Capture current state:
```bash
scripts/harness-eval.sh                           # default: specs 057, 075, 081
scripts/harness-eval.sh --all                     # every spec with tasks.md
scripts/harness-eval.sh --compare NNN /tmp/x.md   # PASS/FAIL verdict
```

**Validation (2026-04-16):** Sonnet with `prompts/decompose-spec.md` produced functionally-equivalent-or-better decompositions vs hand-crafted baselines on specs 057 (62 tasks vs 38) and 081 (55 tasks vs 52). 100% annotation coverage, all FRs mapped, all user stories covered, QA tiers present. Opus wrapper not required.

## Rollback

If the pipeline degrades:

1. **Disable `/spec-decompose`** — rename `.claude/skills/spec-decompose/SKILL.md` → `.md.disabled`
2. **Disable hook orchestrator** — remove from `.claude/settings.json` PostToolUse
3. **Revert custom tasks template** — `git checkout .specify/templates/tasks-template.md`
4. **Existing 18+ specs remain untouched** — no migration needed
5. **Fall back to hand-crafted tasks.md** — no tooling required

## Kill criteria per tool

| Tool | Kill criterion |
|------|---------------|
| spec-kit | Abandoned by GitHub Labs OR Anthropic ships native spec management |
| ruflo (claude-flow@v3alpha) | Two breaking changes in 30 days OR stable 1.0 not cut by Q4 2026 |
| `/spec-decompose` | Sonnet quality drops below 80% on 3 consecutive specs |
| custom tasks-template.md | spec-kit changes tasks.md contract in an incompatible way |
| post-spec-write.sh | Anthropic ships native task orchestration / Linear sync |

## QA Ralph Loop (per-phase QA)

Added in harness v3. Runs automatically inside `/feature-implement` when `--qa-loop` is enabled (default).

```
Phase N tasks complete
        │
        ▼
┌─ Deterministic hooks ─┐
│  vitest (TS/JS files)  │
│  pytest (Python files)  │
└────────┬───────────────┘
         │
         ▼
┌─ LLM QA swarm (ruflo) ─┐
│  qa-e2e (browser)       │
│  qa-review (code review)│
│  qa-security (OWASP)    │
└────────┬───────────────┘
         │
    pass? ──yes──▶ Phase N+1
         │
        no
         │
         ▼
┌─ Investigate + fix ────┐
│  /investigate (5 Whys)  │
│  fix sub-agent          │
│  /qa-only (re-verify)   │
└────────┬───────────────┘
         │
    retry < 3? ──yes──▶ Re-run QA
         │
        no
         │
         ▼
    Mark [F], stop pipeline
```

See `docs/harness/qa-ralph-loop.md` for configuration and cost details.

## References

- [decompose-spec.md](../../prompts/decompose-spec.md) — the canonical Sonnet prompt
- `scripts/harness-eval.sh` — baseline + compare script (not included in ralph; project-specific)
- `scripts/hooks/post-implement-batch.sh` — PostToolUse auto-QA hook
- [spec-decompose SKILL.md](../../skills/spec-decompose/SKILL.md) — the skill
- `specs/*/tasks.md` — use any existing tasks.md as format reference
- `examples/000-qa-ralph-synthetic/tasks.md` — minimal example included in ralph package
