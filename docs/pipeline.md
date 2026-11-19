# Agent Harness Pipeline

End-to-end flow from feature idea to production, using gstack + spec-kit + gsd-core + host adapters.

**Last updated:** 2026-11-19
**Maintainer:** ralph contributors
**Kill criteria:** see [Kill criteria per tool](#kill-criteria-per-tool)

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
│   /feature-spec NNN  (chains the steps below)                          │
│    │   /speckit.specify ──► specs/NNN-name/spec.md                     │
│    │   socratic self-interrogation ──► specs/NNN-name/socratic.md      │
│    │   /speckit.plan ──► specs/NNN-name/plan.md                        │
│    │   /autoplan  ──►  reviews plan.md in place (skipped when the      │
│    │                   skill is not installed)                         │
│    │   /speckit.clarify  (skip with --no-clarify)                      │
│    │   /spec-decompose  ──►  seeds .planning/ (PROJECT, REQUIREMENTS,  │
│    │                    ROADMAP, config.json) and drives               │
│    │                    /gsd-plan-phase: research → plans → checker    │
│    │                    edge-probe gate writes edge-coverage.md        │
│    │   preflight  ──►  specs/NNN/preflight.json  (--no-preflight)      │
│    │   autonomy grant  ──►  grant ledger  (--gated, --no-grant)        │
│    │                                                                   │
│    ▼                                                                   │
│   /feature-implement [NNN]   (gsd-core loop)                           │
│    │   per phase: requirement-ownership gate, plan-probe gate, then    │
│    │   plan-wall (adversarial plan review, always on,                  │
│    │   producer≠reviewer) must pass before that phase executes         │
│    │   gates.py evidence is the completion authority                   │
│    │   flags: --dry-run, --autonomous, --adhoc, --no-finish            │
│    │                                                                   │
│    ▼                                                                   │
│   finish tail (default; --no-finish opts out), in order:               │
│   canary-gate (headless browser QA when the diff touches web surfaces) │
│   openwiki stage (stages openwiki/ updates; skipped without openwiki/) │
│   /review-gate ──►  cross-model 3-pass review                          │
│   /ship   ──►  PR (grant-walled; merge needs a merge:pr grant)         │
│   /canary ──►  post-ship smoke                                         │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

## Key simplification (2026-04-16)

- `/office-hours` produces the problem framing
- `/feature-spec NNN` chains `/speckit.specify`, socratic self-interrogation, `/speckit.plan`, `/autoplan` (skipped when not installed), `/speckit.clarify`, `/spec-decompose`, preflight and the autonomy grant
- `/spec-decompose` stops unless `specs/NNN-name/plan.md` exists; it takes REQ ids from the acceptance criteria in spec.md
- `/autoplan` reviews the plan in place
- `/spec-decompose` (v2) seeds `.planning/PROJECT.md`, `REQUIREMENTS.md`, `ROADMAP.md` and `config.json` from spec.md and plan.md, then drives `/gsd-plan-phase`. It writes no tasks.md. Before plan-phase it runs an edge-probe spec-completeness gate over `.planning/REQUIREMENTS.md` (8-category boundary-value taxonomy), writing `specs/NNN-name/edge-coverage.md` and soft-gating on unresolved edges. A size-aware ceremony tier (`full`, `light`, `adhoc`) decides whether the run uses per-phase walls or one run-level wall
- `/feature-implement` runs the seeded gsd phases through the gsd-core loop; model routing comes from the gsd config in `.planning/config.json`; any package install first clears a package-legitimacy gate (registry-existence check + optional `slopcheck`) — `SLOP` hard-blocks, `SUS`/`[ASSUMED]` routes through the autonomy-grant ledger as `install:<pkg>`
- Before each phase executes, `scripts/gsd/plan-wall.sh` runs an always-on adversarial review of that phase's plan with a model distinct from the planner's (see [Model tiers](model-tiers.md#the-plan-wall)). It is one round: an unresolved CRITICAL finding blocks and buys one repair round, HIGH-only findings pass as `PASS-RESIDUAL` and are checked again at the executed-diff review. A completion backstop in `gates-test-command.sh` refuses to let a phase finish without a passing wall record
- The shared workflow is host-neutral; Claude and Codex only affect how the skills are invoked and which model ladder is selected

## The user gates

At each gate, the user reviews the generated artifact and approves or iterates:

| Gate | After | Artifact | Decision |
|------|-------|----------|----------|
| 1 | /office-hours | design doc | premise + wedge correct? |
| 2 | /speckit.plan (inside /feature-spec) | plan.md | tech approach sound? |
| 3 | /autoplan | reviewed plan.md | accept challenges + taste decisions? |
| 4 | /spec-decompose | `.planning/ROADMAP.md` + phase plans | decomposition atomic + deps right? |
| 5 | /feature-implement (per phase, via gsd-core) | phase evidence | tests pass, behavior verified? |
| 6 | staging deploy | live URL | promote to prod? |

**`/speckit.specify`**: `/feature-spec` runs it first and writes spec.md (user stories, BDD scenarios, acceptance criteria). /spec-decompose reads spec.md for requirement ids and plan.md for the phase breakdown. The deprecated `/feature` stub chains `/feature-spec` then `/feature-implement NNN --auto`; the run is attended, and the stub does not pass `--autonomous`.

## Optional: `/swarm --cloud`

`/swarm --cloud` sits outside this pipeline and outside the gsd-core loop. It is an opt-in flag on the ad-hoc `/swarm` skill only; `/feature-implement` and `/task-swarm` do not support it yet. It offloads eligible, well-scoped tasks to cloud sessions and builds a local integration branch for you to merge. It has its own cross-vendor review and sandboxed test steps, described there. See [Cloud delegation](cloud-delegation.md).

## Commands quick reference

### Planning layer

| Command | Purpose | Output |
|---------|---------|--------|
| `/office-hours` | Problem framing, premise challenge, Codex steelman | `~/.gstack/projects/$SLUG/{user}-{branch}-design-{dt}.md` |
| `/feature-spec NNN` | Chains specify, socratic, plan, autoplan, clarify, spec-decompose, preflight and grant (flags: `--gated`, `--no-clarify`, `--no-preflight`, `--no-grant`, `--no-swarm`, `--dry-run`) | spec.md, socratic.md, plan.md, `.planning/`, preflight.json, grant ledger |
| `/speckit.specify "X"` | Generate spec.md from natural language; creates branch | `specs/NNN-feature-name/spec.md` + git branch |
| `/speckit.clarify` | Q&A to resolve spec ambiguities | mutates spec.md |
| `/speckit.plan "tech stack"` | Generate plan.md + research.md + contracts | `specs/NNN/plan.md` + support files |
| `/autoplan` | Dual-voice review (CEO + Eng + DX + optional Design); `--accept-all-recommendations` auto-selects every answer | mutates plan.md with Decision Audit Trail |
| `/speckit.analyze` | Cross-artifact consistency check | report only |
| `/spec-decompose [NNN]` | Seed the gsd project from spec.md + plan.md and drive `/gsd-plan-phase` (research, plans, plan-checker) | `.planning/{PROJECT,REQUIREMENTS,ROADMAP}.md`, `config.json`, phase plans, `specs/NNN/edge-coverage.md` |

### Execution layer

| Command | Purpose | Status |
|---------|---------|--------|
| `/feature [NNN]` | **DEPRECATED** — flagless stub chaining `/feature-spec` → `/feature-implement NNN --auto` (attended; it does not pass `--autonomous`). See [Commands](commands.md) | available |
| `/feature-implement [NNN]` | **Default: run the current phase via the gsd-core loop.** Model routing from the gsd config in `.planning/config.json` | available |
| `/feature-implement [NNN] --autonomous` | Unattended: preflight PASS + grant ledger required | available |
| `/feature-implement [NNN] --dry-run` | Print resolved phase + gates without executing | available |
| `/feature-implement --adhoc "<task>"` | No spec or plan: same walls and finish tail over `/gsd-quick` | available |
| `/feature-implement [NNN] --no-finish` | Skip the finish tail (browser gate, review-gate, ship, canary) | available |
| `/speckit.implement` | Upstream spec-kit executor (generic; does not use the gsd plans or walls) | available |

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
| Seeded gsd project | `.planning/PROJECT.md`, `REQUIREMENTS.md`, `ROADMAP.md`, `config.json` | `/spec-decompose` |
| Phase plans | `.planning/phases/NN-name/*-PLAN.md` | `/gsd-plan-phase`, driven by `/spec-decompose` |
| Edge coverage | `specs/NNN-feature-name/edge-coverage.md` | `/spec-decompose` (edge-probe gate) |
| Preflight | `specs/NNN-feature-name/preflight.json` | `/preflight`, run by `/feature-spec` |
| Tasks (legacy) | `specs/NNN-feature-name/tasks.md` | retired `/spec-decompose` v1; old files stay readable |

Dropped rows: Design doc / CEO plan / Eng test plan / Autoplan restore
(`~/.gstack/projects/...`) had zero in-repo wiring to corroborate the path
format — gstack owns that convention, not this package. Harness state/log and
Eval baseline pointed at `scripts/hooks/post-spec-write.sh` and
`scripts/harness-eval.sh`, neither of which exists anywhere in this repo.

## Custom tasks.md format (legacy)

`/spec-decompose` v2.0.0 stopped writing tasks.md and `/feature-implement` no longer executes it. The format below is kept for old specs and for [prompts/decompose-spec.md](../prompts/decompose-spec.md), which carries a LEGACY banner. Live model routing is in the gsd config, not in task annotations.

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

## QA tiers (legacy tasks.md phases)

| Tier | Environment | Tests | Responsibility |
|------|-------------|-------|----------------|
| Unit | localhost | vitest / pytest | implementer |
| Integration | localhost | vitest with real DB | implementer |
| E2E | localhost | Playwright | qa-engineer agent |
| Dev QA | localhost | manual dogfood per story | qa-engineer agent |
| Staging | live staging URL | smoke + 24h soak | qa-engineer agent |
| Production | live prod URL | canary + 1h monitor | devops-engineer agent |

## Rollback

If the pipeline degrades, turn off the part that misbehaves. Each switch below exists in the shipped skills and scripts:

1. **Plan wall** — `PLAN_WALL=off` skips the wall. It writes a durable waiver record, and the skip fails closed if the record cannot be written.
2. **Plan adversary** — `PLAN_ADVERSARY=off` skips the cross-model review of high-blast plans during `/spec-decompose`.
3. **Finish tail** — `/feature-implement NNN --no-finish` skips the browser gate, `/review-gate`, ship and canary. This is an operator-accepted risk.
4. **Spec steps** — `/feature-spec NNN` accepts `--no-clarify`, `--no-preflight`, `--no-grant` and `--no-swarm`.
5. **Run finalizer** — `FFS_RUN_FINALIZER=off` disables the run-end cleanup.
6. **Old specs** — existing `specs/*/tasks.md` files stay readable history. No migration is needed.

## Kill criteria per tool

| Tool | Kill criterion |
|------|---------------|
| spec-kit | Abandoned by GitHub Labs OR Anthropic ships native spec management |

The v1 criteria for the Sonnet decomposition prompt and the custom tasks template no longer apply, because `/spec-decompose` v2.0.0 retired both.

## QA Ralph Loop (per-phase QA)

`/feature-implement` runs the gsd-core gate ladder (`/gsd-execute-phase N`) per
phase — there is no `--qa-loop`/`--no-qa-loop` flag; this gate always runs. The
diagram below shows the design of the separate `scripts/qa-swarm.sh` plus
`scripts/ralph-retry.sh` cycle. The only live caller of `qa-swarm.sh` is the
background auto-QA hook, which runs one review pass with no retry, and nothing
calls `ralph-retry.sh`. See [qa-ralph-loop.md](qa-ralph-loop.md) for the real
mechanism.

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
    Stop and report
```

See [qa-ralph-loop.md](qa-ralph-loop.md) for configuration and cost details.

## References

- [decompose-spec.md](../prompts/decompose-spec.md) — the LEGACY v1 decomposition prompt (kept for reference and the hybrid agent catalog)
- `scripts/hooks/post-implement-batch.sh` — PostToolUse auto-QA hook
- [spec-decompose SKILL.md](../skills/spec-decompose/SKILL.md) — the skill
- `specs/*/tasks.md` — legacy tasks.md files, as format reference only
- `examples/000-qa-ralph-synthetic/tasks.md` — minimal example included in ralph package
