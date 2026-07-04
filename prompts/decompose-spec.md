# Prompt: Decompose a Feature Spec into an Executable Task List

Copy this prompt verbatim into your conversation, then reference `specs/NNN-feature-name/spec.md` and `specs/NNN-feature-name/plan.md`. Use the host-appropriate model ladder for decomposition: Claude Code emits `haiku` / `sonnet` / `opus` (plus the optional Claude-Code-native `fable` tier); Codex OAuth emits `gpt-5.4-mini` / `gpt-5.4` / `gpt-5.5`.

---

## Role

You are a senior engineer decomposing an approved feature spec into an atomic, TDD-ordered, executable task list. Your output will be consumed by multiple implementing agents. Clarity, specificity, and correct ordering determine whether the implementation succeeds or drifts into tunnel vision.

**You are not writing code.** You are producing `specs/NNN-feature-name/tasks.md` — a checklist that turns approved design into discrete, verifiable work units.

## Host-aware model ladder

- Claude Code: `haiku` / `sonnet` / `opus` / `fable` (optional 4th tier, see below)
- Codex OAuth: `gpt-5.4-mini` / `gpt-5.4` / `gpt-5.5`
- Emit the native identifiers for the host you are running in; do not translate between ladders.
- `fable` is Claude-Code-native only — no Codex equivalent. Reserve it for tasks that need
  multi-file narrative coherence: reconciling voice/structure across several files in one
  pass (a docs rewrite spanning 5+ files, a cross-module rename narrative, a brand-voice
  consistency pass). Emit it sparingly — most tasks belong on `sonnet`. `/feature-implement`
  and `/swarm` execute it on the native `Task()` path; on the Ruflo-coordinated path it
  silently downgrades to `sonnet` (Ruflo's model enum is `haiku`\|`sonnet`\|`opus`\|`inherit`
  only — no `fable` slot).

## Inputs (read first, in order)

**Required:**
1. `specs/NNN-feature-name/plan.md` — tech stack, architecture, file structure, chosen approach

**Optional (but strongly preferred if present):**
2. `specs/NNN-feature-name/spec.md` — user stories (P1, P2, P3), acceptance scenarios, FR, SC, entities
3. `specs/NNN-feature-name/research.md` — library decisions, version pins
4. `specs/NNN-feature-name/data-model.md` — entities and relationships
5. `specs/NNN-feature-name/contracts/` — API/interface specs
6. `~/.gstack/projects/$SLUG/*-design-*.md` — gstack office-hours design doc (problem framing, premises, wedge)

**Format references (always read):**
7. Any existing `specs/*/tasks.md` in the project — as format reference (should have [model:], [agent:], [US] annotations)
8. `examples/000-qa-ralph-synthetic/tasks.md` (from ralph package) — minimal reference
9. `CLAUDE.md` — project conventions, verification commands, TDD rules

Do not start writing until you have read all inputs that exist.

### Handling missing spec.md

If `spec.md` is missing, extract user stories from whichever source has them:
- **First choice:** look for a `## User Stories` or `## Use Cases` section in `plan.md`
- **Second choice:** look in the gstack `*-design-*.md` for "Target User" / "Narrowest Wedge" sections
- **Fallback:** treat the entire feature as a single user story (US1) with priority P1

If none of the above yield user stories, tag ALL implementation tasks with `[US1]` and produce a single-phase feature. This is acceptable for small focused features; it's suboptimal for multi-story features but better than refusing to decompose.

## Output

Single file: `specs/NNN-feature-name/tasks.md`

## Task line format (strict)

```
- [ ] T### [P?] [US?] [model:X thinking:Y] [agent:exact-agent] Description with `exact/file/path.ext`
```

Components in order:

| Field | Required | Example | Notes |
|---|---|---|---|
| `- [ ]` | yes | `- [ ]` | Markdown checkbox. `[X]` = done, `[F]` = failed, `[S]` = skipped |
| `T###` | yes | `T001`, `T042` | Sequential, zero-padded, execution order |
| `[P]` | optional | `[P]` | Parallel-safe: different files, no deps on incomplete tasks |
| `[USn]` | if user-story phase | `[US1]` | Ties to spec.md user story priority; NO story label for Setup/Foundational/Integration/QA phases |
| `[model:X]` | yes | `[model:sonnet]` | Host-specific ladder — Claude: `haiku` \| `sonnet` \| `opus` \| `fable`; Codex: `gpt-5.4-mini` \| `gpt-5.4` \| `gpt-5.5` (`fable` is Claude-only, downgrades to `sonnet` on the Ruflo path) |
| `[thinking:Y]` | yes | `[thinking:med]` | `low` \| `med` \| `high` \| `max` — thinking budget for implementer |
| `[agent:exact-agent]` | yes | `[agent:ecc:tdd-guide]` | Exact label from the hybrid ECC + wshobson catalog; keep it stable |
| `[return:X]` | optional | `[return:deep]` | Return contract: `scout` (≤15-line facts report) \| `build` (≤20-line change report) \| `deep` (≤40-line conclusion-first report). Omit to derive from model tier (low→scout, mid→build, high→deep). Annotate only when the tier default is wrong — e.g. a low-tier model doing an audit that must report deep |
| Description | yes | `Implement POST /api/auth in `web/src/app/api/auth/route.ts`` | Concrete action + backticked file path from repo root |

Append an optional second line for dependencies:
```
      Depends-on: T005, T012
```

Two-space indent, `Depends-on:` prefix, comma-separated task IDs. Omit if no upstream dependency.

## Model and thinking tier guidance

| Tier | Use for | Examples |
|---|---|---|
| `[model:haiku thinking:low]` / `[model:gpt-5.4-mini thinking:low]` | Boilerplate, migrations, renames, deps install | `Install shadcn/ui`, `Run migration 007`, `Rename service X to Y` |
| `[model:sonnet thinking:med]` / `[model:gpt-5.4 thinking:med]` | Default — most logic, routes, tests, components | Writing a POST route, writing a unit test, building a React component |
| `[model:sonnet thinking:high]` / `[model:gpt-5.4 thinking:high]` | Complex logic, edge cases, cross-module integration | Writing E2E tests with Playwright, solving race conditions, reconciling DB + cache |
| `[model:opus thinking:max]` / `[model:gpt-5.5 thinking:max]` | Architecture decisions, complex debugging, ambiguous specs | Designing a new subsystem, investigating intermittent test failures, choosing between two fundamental approaches |
| `[model:fable thinking:high]` (Claude Code only) | Multi-file narrative/voice coherence | Rewriting docs consistently across 5+ files, a brand-voice pass over multiple pages, reconciling naming/tone across a module rename |

**Default to the middle tier for the current host** (`sonnet/med` on Claude Code, `gpt-5.4/med` on Codex). Escalate only with justification (add a one-line comment above the task if escalating to the highest tier or `thinking:max`).

## Agent routing (hybrid catalog)

Use the most specific exact agent label available. Prefer ECC for TDD, review, architecture, cleanup, and analysis; prefer wshobson specialists for implementation, debugging, UI/UX, infra, docs, and language-specific work.

### Preferred mappings

| Need | Exact agent label | Use for |
|---|---|---|
| Test-first decomposition | `ecc:tdd-guide` | RED test tasks, TDD ordering, coverage work |
| Code review / quality gate | `ecc:code-reviewer` | Diff review, lint/consistency review, refactor safety |
| Broad architecture | `ecc:architect` | Cross-cutting design, system boundaries, coupling |
| Spec extraction | `ecc:spec-miner` | Pulling user stories and requirements from sparse inputs |
| Docs cleanup | `ecc:doc-updater` | Editing reference docs and release notes |
| Build/test failure triage | `ecc:build-error-resolver` | CI breakage, toolchain failures |
| Silent failures / flaky behavior | `ecc:silent-failure-hunter` | Intermittent or non-deterministic regressions |
| Refactor / cleanup | `ecc:refactor-cleaner` | Mechanical cleanup after the main fix lands |
| Backend API architecture | `backend-architect` | Service boundaries, backend module layout, API shape |
| GraphQL APIs | `graphql-architect` | Schema design, resolvers, GraphQL boundaries |
| Frontend implementation | `frontend-developer` | UI code, React components, page wiring |
| UI/UX design | `ui-ux-designer` | Interaction design, flow polish, layout decisions |
| Accessibility | `accessibility-expert` | WCAG, keyboard nav, screen-reader behavior |
| Visual verification | `ui-visual-validator` | Pixel/visual regression and layout sanity checks |
| Browser automation | `test-automator` | Playwright-style E2E and browser workflows |
| Security review | `ecc:security-reviewer` | Secure-by-default review, auth/data-handling checks |
| Security audit | `security-auditor` | Independent audit pass, threat modeling, security review |
| Security-coded implementation | `backend-security-coder` | Safer backend changes in auth/data paths |
| Frontend security-coded implementation | `frontend-security-coder` | Safer UI/input handling changes |
| TypeScript | `typescript-pro` | TS services, app code, utility layers |
| JavaScript | `javascript-pro` | JS glue, runtime scripts, lightweight app code |
| Python | `python-pro` | Python scripts, automation, backend utilities |
| FastAPI | `fastapi-pro` | FastAPI routes, dependencies, request/response models |
| Django | `django-pro` | Django views, ORM, admin, middleware |
| Java | `java-pro` | JVM services and tooling |
| Go | `golang-pro` | Go services, CLIs, and platform code |
| Rust | `rust-pro` | Rust systems code and performance-sensitive paths |
| C# | `csharp-pro` | .NET services and tooling |
| PHP | `php-pro` | PHP services and legacy app support |
| SQL / data | `sql-pro` | Query design, schema-aware SQL, data work |
| Database schema | `database-architect` | Schema changes, tables, relationships, migrations |
| Query tuning | `database-optimizer` | Slow queries, indexing, execution plans |
| Database admin | `database-admin` | Backups, permissions, maintenance, operational tasks |
| Performance work | `performance-engineer` | Profiling, latency, throughput, resource use |
| Debugging | `debugger` | Reproducing and isolating bugs fast |
| Production incidents | `incident-responder` | Live incident triage and recovery |
| Deep bug hunting | `error-detective` | Stack traces, heisenbugs, non-obvious regressions |
| Deployment / release engineering | `deployment-engineer` | Ship pipeline, rollout logic, deployment scripts |
| Cloud architecture | `cloud-architect` | Infra boundaries and cloud services |
| Kubernetes | `kubernetes-architect` | K8s manifests, deployments, service topology |
| Terraform | `terraform-specialist` | IaC, providers, state, modules |
| Observability | `observability-engineer` | Logs, metrics, traces, alerting |
| Networking | `network-engineer` | Routing, connectivity, proxies, DNS, transport |
| Docs architecture | `docs-architect` | Doc site structure, docs IA, canonical narrative flow |
| API docs | `api-documenter` | OpenAPI/spec docs, request/response examples |
| Reference docs | `reference-builder` | Long-form references and canonical docs |
| Reverse engineering | `reverse-engineer` | Understanding opaque or legacy code paths |
| Context synthesis | `context-manager` | Cross-file summarization and context packing |
| Prompting / delegation | `prompt-engineer` | Crafting prompts and agent instructions |
| Business analysis | `business-analyst` | Requirements, tradeoffs, and priority framing |
| Sales automation | `sales-automator` | Pipeline automation and outbound workflows |
| Customer support | `customer-support` | User-facing support flows and replies |
| SEO metadata | `seo-meta-optimizer` | Metadata, titles, descriptions, search snippets |

If the task does not map cleanly, choose the nearest specialist above rather than a generic label. If you are unsure between two adjacent roles, prefer the one that matches the file type or artifact being changed.

## Explicit E2E artifact policy

Do not hide required end-to-end coverage behind generic "run QA" tasks.

- Browser or other user-visible flows must include explicit Playwright-style `*.spec.ts` tasks that create or update the actual test file.
- OpenClaw gateway, agent, or channel flows must include explicit `tests/scenarios.yaml` tasks when those surfaces are touched.
- If the spec reaches a live OpenClaw or Telegram surface, include explicit tasks that wire the requested QA lane into the implementation plan rather than leaving it implied.
- Prefer small, atomic test tasks that fail for the intended reason before the corresponding implementation task.

## Review-Gate Phase Gates (MANDATORY)

Every implementation phase in `tasks.md` **MUST** end with a `/review-gate` task before the next
phase begins. This is a hard requirement — a decomposition that omits one from any implementation
phase is invalid.

Use the current host's middle model tier from the ladder above (`sonnet` on Claude Code, `gpt-5.4`
on Codex OAuth) — never `fable` or the top/bottom tiers — for the review-gate task itself.

### Canonical review-gate task format

```
- [ ] T### [model:<host-middle-tier> thinking:med] [agent:ecc:code-reviewer] /review-gate — review Phase N diff. HIGH/CRITICAL findings block Phase N+1. Address all CRITICAL, fix or defer HIGH. [qa:review-gate]
      Depends-on: T### (last implementation task in this phase)
```

`[qa:review-gate]` is a reserved tag distinct from the general `[qa:review]` annotation used
elsewhere in this doc (see QA Dimensions below) — it marks the one task per phase that gates the
phase transition, not an ordinary code-review pass.

### Where to place review-gate tasks

- **After every implementation phase**: Setup, Foundational, each user story (US1…USn), and
  Cross-Story Integration.
- **NOT** after read-only or planning phases (Research, Architecture Review) — nothing to diff.
- **NOT** after Staging Deploy & Soak, Production Promotion, or Rollback Plan phases — those use
  staging smoke tests and canary monitoring instead of `/review-gate`.
- Always the last task in the phase, with `Depends-on:` pointing at that phase's final
  implementation (or Dev QA) task.

## RED-proof pairing (MANDATORY)

Every implementation task MUST be preceded by a paired test-author task
(`[agent:ecc:tdd-guide]`) that writes the failing test. The implementation task
carries `Depends-on:` the test task. At run time the harness blocks the
implementation task until the test task has recorded a RED proof
(`gates.py check-red`) — a test run that actually failed. Decompose accordingly:
never emit an implementation task with no failing-test predecessor.

## Per-story e2e smoke task (MANDATORY)

Every user-story phase MUST end with one e2e smoke task before its review-gate
task: `[agent:test-automator] [qa:e2e]`, description derived from the spec's BDD
Given/When/Then scenarios for that story. A story phase with no e2e task fails
`gates.py analyze` and `/feature-implement` will refuse to start.

## Phase structure (required)

Produce tasks.md in this exact phase sequence:

### Phase 1: Setup
Migrations, dependency installs, scaffolding. No story label. Tasks typically `[P]` after T001.
End the phase with a `/review-gate` task (see Review-Gate Phase Gates above).

### Phase 2: Foundational
Blocking prerequisites for ALL user stories — shared models, middleware, auth, base utilities. Must complete before any user story phase starts. Most tasks `[P]` where files differ.
End the phase with a `/review-gate` task before any user story phase may start.

### Phase 3 through N+2: One phase per user story (P1 first, then P2, P3...)

For each user story phase, structure internally as:

```
## Phase 3: User Story 1 — [Title] (Priority: P1) 🎯 MVP

**Goal:** <one-line user-facing outcome>
**Independent test:** <how you verify this story works standalone>

### Tests for User Story 1 (RED — write first, must fail)
- [ ] T### [P] [US1] [model:sonnet thinking:med] [agent:ecc:tdd-guide] Write unit test for...
- [ ] T### [P] [US1] [model:sonnet thinking:med] [agent:test-automator] Write integration test for...

### Implementation for User Story 1 (GREEN — make tests pass)
- [ ] T### [US1] [model:sonnet thinking:med] [agent:backend-architect] Implement...
      Depends-on: T### (the failing test)

### Dev QA for User Story 1
- [ ] T### [US1] [model:sonnet thinking:high] [agent:ui-visual-validator] Manual dogfood on localhost: happy path + 2 edge cases

### Review Gate for User Story 1
- [ ] T### [US1] [model:sonnet thinking:med] [agent:ecc:code-reviewer] /review-gate — review Phase 3 diff. HIGH/CRITICAL findings block Phase 4. [qa:review-gate]
      Depends-on: T### (Dev QA task above)
```

TDD is non-negotiable: tests come before implementation. Implementation tasks have `Depends-on:` line naming their test tasks. Every user story phase closes with its own `/review-gate` task — do not let one story's phase open before the previous story's gate has passed.

### Phase N+1: Cross-Story Integration (E2E, dev env)
- Full-flow E2E tests combining all user stories
- Responsive/accessibility testing
- Cross-browser smoke
- Close the phase with a `/review-gate` task — this is the last implementation-phase gate before Staging

### Phase N+2: Staging Deploy & Soak
- Deploy to staging env (Vercel preview, Railway staging, etc.)
- Staging smoke test (5 critical paths on live URL)
- 24h soak: error rate must stay < 0.5%
- **Staging sign-off gate** — explicit approval before production

### Phase N+3: Production Promotion
- Merge to main (triggers prod deploy)
- Prod smoke test on live domain
- Canary monitor for 1h — rollback if error rate > 1%
- Post-deploy: update CHANGELOG, close Linear issues

### Phase N+4: Rollback Plan (always include)
- `gh pr revert` command documented
- Vercel previous-build redeploy procedure
- DB migration-down script path (if schema changed)

## Quality criteria (enforce on yourself)

1. **Every task has a file path** — backticked, relative-to-repo-root, ending in an extension or trailing slash. No vague "implement the service."
2. **Every task is atomic** — a single Claude Code session can complete it in 5-30 minutes.
3. **Every implementation task has `Depends-on:` line citing its test task.**
4. **`[P]` only when truly safe** — different files, no shared state, no upstream deps still `[ ]`.
5. **User stories are independently shippable** — User Story 1 alone should deliver MVP value. User Story 2 adds to MVP; doesn't require User Story 3 to exist.
6. **No more than 50 tasks** for a reasonable feature. If you exceed 50, split the feature into multiple specs.
7. **No duplicate work across stories** — shared infrastructure goes in Foundational (Phase 2).
8. **Every implementation phase ends with a `/review-gate` task** — Setup, Foundational, each user story, and Cross-Story Integration. No exceptions (see Review-Gate Phase Gates).

## After writing the task list, produce these sections

```markdown
## Dependencies & Execution Order

<ASCII diagram or bullet list of phase order and critical-path through user stories>

## Parallel Execution Groups

- Phase 2: T005, T006, T007 (all [P], different files)
- US1 tests: T010-T013 ([P] block)
- US1 implementation: T014-T016 (sequential on same service)

## Implementation Strategy

**MVP (Phase 1 → 3):** User Story 1 alone ships as v0.1. Close Linear issue, merge PR.

**Increment 2 (Phase 4):** Add User Story 2. Separate Linear issue, separate PR.

**Staging gate (S004):** Blocks production promotion. Never skip.

**Canary gate (P003):** Blocks next feature. Green for 1h before merging anything else.

**Review-gate count:** report one `/review-gate` task per implementation phase (Setup, Foundational, each user story, Cross-Story Integration) — flag any phase missing one before declaring the decomposition done.
```

## QA Dimensions

Each task may include a `[qa:unit,integration,e2e,review,security]` annotation specifying which QA dimensions run after the phase containing that task completes. This is informational — the QA swarm reads it to decide what to check.

**Defaults (when no [qa:] annotation):**
- Tasks touching TypeScript/JavaScript/Python files: `[qa:e2e,review,security]` (unit + integration run automatically via deterministic hooks)
- Tasks touching only config/docs/SKILL.md: `[qa:review]` (only code review, no tests)

**When to customize:**
- Pure backend tasks with no UI: `[qa:review,security]` (skip e2e)
- Tasks modifying auth/payments/user-data: `[qa:review,security]` (security is critical, e2e may not cover)
- Tasks adding new API endpoints: `[qa:integration,review,security]` (integration tests the contract)

**Do NOT add [qa:unit] or [qa:integration] explicitly** — these dimensions are handled by deterministic test runner hooks (vitest/pytest) that always run. The [qa:] annotation controls only the LLM-based QA agents.

**`[qa:review-gate]` is reserved** for the one mandatory end-of-phase `/review-gate` task described in Review-Gate Phase Gates above. Do not attach it to any other task, and do not use `[qa:review]` as a substitute for it — `[qa:review]` marks ordinary code-review coverage on an implementation task, while `[qa:review-gate]` marks the phase-blocking gate itself.

## Self-check before declaring done

Run the machine coherence gate yourself and fix findings before handing off:

```bash
python3 lib/gates.py analyze specs/NNN/spec.md specs/NNN/tasks.md
```

Checks: every spec US has tasks · every task US exists in the spec · every phase
has a `[qa:review-gate]` task · every story phase has an e2e smoke task.


- [ ] All user stories (P1, P2, P3...) mapped to at least one phase (if spec.md exists; otherwise single US1 phase is OK)
- [ ] All functional requirements (FR-NNN) covered by at least one task (if spec.md exists)
- [ ] All success criteria (SC-NNN) testable via at least one QA task (if spec.md exists)
- [ ] TDD ordering: every implementation task has a preceding test task
- [ ] `[P]` markers only on truly independent tasks (re-verify!)
- [ ] All `Depends-on:` references point to valid task IDs
- [ ] File paths are absolute-from-repo-root and include extensions
- [ ] Phase N+2 has an explicit staging gate
- [ ] Phase N+3 has explicit canary monitoring + rollback
- [ ] Phase N+4 rollback plan is executable (not aspirational)
- [ ] Every implementation phase (Setup, Foundational, each user story, Cross-Story Integration) ends with a `/review-gate` task carrying `[agent:ecc:code-reviewer]`, `[qa:review-gate]`, and a `Depends-on:` line to that phase's last task
- [ ] No `/review-gate` task placed after read-only/planning phases or after Staging/Production/Rollback phases

## Reference: gold-standard phase structure

Read any existing `specs/*/tasks.md` in the project before generating. A good reference has:
- 6 phases, 38 tasks
- Phase 1 (Setup): 5 tasks, mostly [P]
- Phase 2 (Backend API + TDD): tests → implementation split
- Phases 3-5 (Frontend, per-story): tests before UI implementation
- Phase 6 (Polish & Integration): E2E + docs + final verification
- Dependencies section at end
- Parallel Execution section at end
- Implementation Strategy with MVP → Increments

Match its density and specificity. Do NOT match what it lacks: `[model:]`, `[thinking:]`, `[agent:]` annotations, explicit staging/production phases, and `Depends-on:` lines. Your output must include all of those.

## Common failure modes to avoid

1. **Vague descriptions** — "Implement auth" instead of "Implement POST /api/auth in `web/src/app/api/auth/route.ts` with email+password schema validation via Zod"
2. **Missing test pair** — implementation task with no preceding RED test
3. **Over-parallelism** — marking `[P]` when tasks share a file or service
4. **Phase inflation** — splitting a 5-task phase into three 2-task phases
5. **Skipping QA tiers** — ending at user stories without Integration/Staging/Production phases
6. **Dependencies in prose only** — "this depends on T012" in description instead of `Depends-on: T012` line
7. **Using opus for boilerplate** — `[model:opus]` on a `CREATE TABLE` migration is waste
8. **Forgetting rollback** — no Phase N+4 means no recovery plan
9. **Missing a review-gate task** — ending an implementation phase without `/review-gate` lets HIGH/CRITICAL findings carry silently into the next phase; the decomposition is invalid until every implementation phase has one
10. **Overusing `fable`** — reaching for the narrative-coherence tier on ordinary single-file work; reserve it for genuine multi-file voice/structure reconciliation

Start by reading spec.md and plan.md. Then read 057/tasks.md to calibrate depth. Then produce the task list.
