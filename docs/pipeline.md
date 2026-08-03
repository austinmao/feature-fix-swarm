# Agent Harness Pipeline

End-to-end flow from feature idea to production, using gstack + spec-kit + gsd-core + host adapters.

**Last updated:** 2026-07-29
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
│    │                    (host-aware model ladder + prompts/decompose-spec.md) │
│    │                    custom format: [model:] [agent:exact-agent] [US] [P] │
│    │                    QA tiers: unit/int/E2E + dev/staging/prod      │
│    │                    spec.md OPTIONAL — falls back to plan.md       │
│    ▼                                                                   │
│   [consumer-repo hooks, if wired — no FFS-shipped spec-write hook]     │
│    │   ├─ linear-auto-sync (child issues)                              │
│    │   └─ post-task-complete (detect [X] transitions)                  │
│    │                                                                   │
│    ▼                                                                   │
│   /feature-implement [NNN]                                             │
│    │   per phase: plan-wall (adversarial plan review, always on,       │
│    │   producer≠reviewer) must pass before that phase's tasks start    │
│    │   iterates tasks.md; spawns Agent per [model:] annotation         │
│    │   sequential execution, updates [ ] → [X] or [F] on completion    │
│    │   flags: --dry-run, --autonomous, --adhoc, --no-finish            │
│    │                                                                   │
│    ▼                                                                   │
│   /qa     (built INTO tasks.md phases — Dev QA per story)              │
│   /review-gate ──►  cross-model 3-pass review (default-on)              │
│                    skip with --no-review-gate                           │
│   /ship   ──►  PR + staging deploy                                     │
│   /canary ──►  production monitor + rollback                           │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

## Key simplification (2026-04-16)

- `/office-hours` produces the problem framing
- If no spec/plan context exists yet, `/speckit.specify` and `/speckit.plan` bootstrap it before `/autoplan`
- `spec.md` is **optional** — `/spec-decompose` extracts user stories from plan.md or treats the whole feature as a single story when missing
- `/autoplan` reviews the plan (works on any file path)
- `/spec-decompose` produces a normalized tasks.md with host-aware model labels; before plan-phase it also runs an edge-probe spec-completeness gate over `.planning/REQUIREMENTS.md` (8-category boundary-value taxonomy), writing `edge-coverage.md` and soft-gating on unresolved edges
- `/feature-implement` executes tasks one at a time with per-task model routing; any package install first clears a package-legitimacy gate (registry-existence check + optional `slopcheck`) — `SLOP` hard-blocks, `SUS`/`[ASSUMED]` routes through the autonomy-grant ledger as `install:<pkg>`
- Before each phase's tasks start, `scripts/gsd/plan-wall.sh` runs an always-on adversarial review of that phase's plan with a model distinct from the planner's (see [Model tiers](model-tiers.md#the-plan-wall)); HIGH/CRITICAL findings block until adjudicated, and a completion backstop in `gates-test-command.sh` refuses to let a phase finish without a passing wall record
- The shared workflow is host-neutral; Claude and Codex only affect how the task graph is rendered and which model ladder is selected

## The user gates

At each gate, the user reviews the generated artifact and approves or iterates:

| Gate | After | Artifact | Decision |
|------|-------|----------|----------|
| 1 | /office-hours | design doc | premise + wedge correct? |
| 2 | write plan.md (manual or /speckit.plan) | plan.md | tech approach sound? |
| 3 | /autoplan | reviewed plan.md | accept challenges + taste decisions? |
| 4 | /spec-decompose | tasks.md | decomposition atomic + deps right? |
| 5 | /feature-implement (per phase, via gsd-core) | tasks done | tests pass, behavior verified? |
| 6 | staging deploy | live URL | promote to prod? |

**Optional `/speckit.specify`**: if you want user stories in a separate spec.md, run it after /office-hours. If no spec/plan context exists yet, `/feature` bootstraps it first. /spec-decompose will use the spec.md when present; if you skip it, /spec-decompose falls back to plan.md or treats the feature as a single user story.

## Commands quick reference

### Planning layer

| Command | Purpose | Output |
|---------|---------|--------|
| `/office-hours` | Problem framing, premise challenge, Codex steelman | `~/.gstack/projects/$SLUG/{user}-{branch}-design-{dt}.md` |
| `/speckit.specify "X"` | Generate spec.md from natural language; creates branch | `specs/NNN-feature-name/spec.md` + git branch |
| `/speckit.clarify` | Q&A to resolve spec ambiguities | mutates spec.md |
| `/speckit.plan "tech stack"` | Generate plan.md + research.md + contracts | `specs/NNN/plan.md` + support files |
| `/autoplan` | Dual-voice review (CEO + Eng + DX + optional Design); `--accept-all-recommendations` auto-selects every answer | mutates plan.md with Decision Audit Trail |
| `/speckit.analyze` | Cross-artifact consistency check | report only |
| `/spec-decompose [NNN]` | Generate normalized tasks.md with host-aware model tiers via canonical prompt | `specs/NNN/tasks.md` |

### Execution layer

| Command | Purpose | Status |
|---------|---------|--------|
| `/feature [NNN]` | **DEPRECATED** — flagless stub chaining `/feature-spec` → `/feature-implement NNN --autonomous`. See [Commands](commands.md) | available |
| `/feature-implement [NNN]` | **Default: run the current phase via the gsd-core loop.** Per-task model routing via `[model:]` annotation | available |
| `/feature-implement [NNN] --autonomous` | Unattended: preflight PASS + grant ledger required | available |
| `/feature-implement [NNN] --dry-run` | Print resolved phase + gates without executing | available |
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
| Spec | `specs/NNN-feature-name/spec.md` | `/speckit.specify` |
| Plan | `specs/NNN-feature-name/plan.md` | `/speckit.plan` |
| Research | `specs/NNN-feature-name/research.md` | `/speckit.plan` |
| Data model | `specs/NNN-feature-name/data-model.md` | `/speckit.plan` |
| Contracts | `specs/NNN-feature-name/contracts/*` | `/speckit.plan` |
| Tasks | `specs/NNN-feature-name/tasks.md` | `/spec-decompose` |

Dropped rows: Design doc / CEO plan / Eng test plan / Autoplan restore
(`~/.gstack/projects/...`) had zero in-repo wiring to corroborate the path
format — gstack owns that convention, not this package. Harness state/log and
Eval baseline pointed at `scripts/hooks/post-spec-write.sh` and
`scripts/harness-eval.sh`, neither of which exists anywhere in this repo.

## Custom tasks.md format

Every task line:
```
- [ ] T### [P?] [US?] [model:X thinking:Y] [agent:exact-agent] Description with `file/path.ext`
      Depends-on: T005, T012
```

**Fields:**
- `[ ]` checkbox → `[X]` done → `[F]` failed → `[S]` skipped
- `T###` sequential zero-padded ID
- `[P]` parallel-safe (optional)
- `[USn]` user-story tag (required in story phases, forbidden in Setup/Integration)
- `[model:X thinking:Y]` tier for implementing agent — `haiku/low`, `sonnet/med` (default), `sonnet/high`, `opus/max`
- `[agent:exact-agent]` exact hybrid-catalog routing hint for sub-agent delegation via the gsd-core executor
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
| `/spec-decompose` | Sonnet quality drops below 80% on 3 consecutive specs |
| custom tasks-template.md | spec-kit changes tasks.md contract in an incompatible way |

## QA Ralph Loop (per-phase QA)

Runs automatically inside `/feature-implement` via the gsd-core gate ladder
(`/gsd-execute-phase N`) — there is no `--qa-loop`/`--no-qa-loop` flag; this
gate always runs. See [qa-ralph-loop.md](qa-ralph-loop.md) for the real
mechanism, including the separate, narrower background auto-QA hook.

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
┌─ LLM QA swarm ─────────┐
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

See [qa-ralph-loop.md](qa-ralph-loop.md) for configuration and cost details.

## References

- [decompose-spec.md](../../prompts/decompose-spec.md) — the canonical Sonnet prompt
- `scripts/hooks/post-implement-batch.sh` — PostToolUse auto-QA hook
- [spec-decompose SKILL.md](../../skills/spec-decompose/SKILL.md) — the skill
- `specs/*/tasks.md` — use any existing tasks.md as format reference
- `examples/000-qa-ralph-synthetic/tasks.md` — minimal example included in ralph package
