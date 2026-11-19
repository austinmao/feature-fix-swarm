# Feature Fix Swarm Commands Reference

Quick reference for all available commands in the feature-fix-swarm harness across Claude Code and Codex runtimes.

## Feature Development Pipeline

| Command | What it does |
|---------|-------------|
| `/office-hours` | Brainstorm product ideas, validate "is this worth building", structured problem statement |
| `/feature-spec NNN` | **Spec-first pipeline:** speckit.specify → speckit.plan → speckit.clarify, each phase enforcing TDD unit test list, BDD Given/When/Then scenarios, and E2E Playwright stubs. Run before `/autoplan`. |
| `/autoplan` | Full review pipeline: CEO + Eng + DX dual voices with Codex, auto-decides taste decisions; `--accept-all-recommendations` auto-selects every recommended answer |
| `/spec-decompose NNN` | Turn `specs/NNN/plan.md` into normalized `tasks.md` with host-aware `[model:]` `[agent:]` `[qa:]` annotations usable on Claude or Codex. Step 2.5 runs an edge-probe spec-completeness gate (8-category boundary-value taxonomy) before plan-phase — writes `edge-coverage.md`, soft-gates on unresolved edges |
| `/plan-decompose "description"` | Turn a description or existing plan into `tasks.md` via autonomous eng review + `/review-gate` — no speckit interview. Faster path when a spec isn't warranted. |
| `/feature-implement NNN` | Execute tasks.md phase-by-phase via the gsd-core loop, with per-phase QA gates. Flags: `--autonomous`, `--dry-run`, `--adhoc "<task>"`, `--no-finish`. Package installs run through a package-legitimacy pre-install gate first — registry-existence check + optional `slopcheck`; `SLOP` hard-blocks, `SUS`/`[ASSUMED]` routes to the autonomy-grant ledger as `install:<pkg>` |
| `/feature NNN` | **DEPRECATED (v3.19.0)** — a stub that chains `/feature-spec` (only when `specs/NNN/tasks.md` is missing) → `/feature-implement NNN --auto`. That run is attended: the stub does not pass `--autonomous`, and `/feature-implement` has no `--auto` flag, so gates prompt normally. Retired flags (`--accept`, `--accept-all-recommendations`, `--goal`, `--qa-openclaw`, `--qa-telegram`, etc.) are no longer translated; passing one prints a retirement notice. Call `/feature-spec` and `/feature-implement` directly instead. |
| `/task-swarm "task"` | Lightweight autonomous pipeline for a bounded task: `plan-decompose` → `preflight` → `autonomy-grant` → `feature-implement --autonomous`. Zero planned stops by default (MAX-AUTH auto-grant). Flags: `--gated` (review the gate list first), `--attended` (skip the grant; gates prompt during implement), `--no-swarm`, `--dry-run`. See [Choosing a command](choosing-a-command.md) |
| `/preflight` | Prove every env var, secret and service the run needs is present and reachable before an unattended run. Reads `specs/NNN/preflight.json`; runs `gates.py preflight`; `--autonomous` needs a recorded PASS less than 24h old. Names only, never values |
| `/autonomy-grant` | Record the typed `type:target` approvals (push, merge, deploy, ...) an unattended run will hit, as a run-bound ledger with a TTL (default 72h, cap 168h). Exact match only; an unlisted action stops and is recorded as pending |
| `/swarm "task description"` | Ad-hoc parallel task executor — no spec dir required. Classifies each task as volume, execution or judgment, runs independent tasks as concurrent host-native subagent waves and dependent tasks serially, then reports task, model, outcome and evidence. Not the gsd-core loop: use `/feature-implement` for feature work. Flags and the experimental `--cloud` offload: [/swarm flags](#swarm-flags) |

**Pipeline order:** `/office-hours` → `/feature-spec NNN` (TDD+BDD+E2E contracts) → `/autoplan` → `/spec-decompose` → `/feature-implement` → `/qa` → `/review` → `/review-gate` → `/ship` → `/land-and-deploy` → `/canary`

### /feature-spec flags

| Flag | Effect |
|------|--------|
| (none) | Full pipeline: speckit.specify → speckit.plan → speckit.clarify → spec-decompose → preflight → autonomy-grant (MAX-AUTH auto-grant) |
| `--gated` | Stop at Step 6 to review the enumerated gate list before granting |
| `--no-clarify` | Stop after speckit.plan; skip clarify phase |
| `--no-preflight` | Skip preflight (not recommended before `--autonomous`) |
| `--no-grant` | Skip the grant ledger entirely (attended runs) |
| `--no-swarm` | Single-planner decomposition instead of the swarm |
| `--dry-run` | Preview what would be generated without writing files |

### /swarm flags

| Flag | Effect |
|------|--------|
| `"task1" "task2" ...` | Classify and execute the listed tasks |
| `--tasks-file PATH` | Read tasks from a markdown checklist or one task per line |
| `--dry-run` | Print the annotated plan; execute nothing |
| `--sequential` | Run serially instead of in parallel waves |
| `--model-request '{"kind":"tier","name":"judgment"}'` | Override the model request for the run |
| `--cloud[=auto\|prefer\|required]` | **Experimental, opt-in.** Offload eligible long tasks to Claude Code on the web or Codex cloud sessions. Bare `--cloud` means `auto`. The `FFS_CLOUD` environment variable also turns it on |
| `--dry-run --cloud` | Run `cloud-dispatch.sh plan --with-doctor`, print groups, routes, providers, refusals and missing grants, then stop. Starts nothing |

Cloud offload is off unless you ask for it. Under `auto` or `prefer`, a refused task runs locally; under `required`, the run stops. Interactive runs ask one grant question; autonomous and headless runs never self-grant. The result is a local branch `ffs/cloud/<run>/integration`; nothing is merged or deployed. `/swarm` has no `--resume` flag. A parked or dead cloud run is resumed with `bash scripts/gsd/cloud-dispatch.sh orchestrate --run <run> --resume`. Full guide: [Cloud delegation](cloud-delegation.md).

## QA + Testing

| Command | What it does |
|---------|-------------|
| `/qa` | Full browser-based QA via `$B` (gstack browse). Finds bugs, takes screenshots, reports. |
| `/qa-only` | Report-only QA. Tests but never fixes. Good for "just tell me what's broken." |
| `/investigate` | Systematic root cause analysis (5 Whys). Scope-locks to affected module. |
| `/tdd` | TDD workflow: write test first (RED), implement (GREEN), refactor — one atomic behavior per cycle |
| `/testing-policy` | The canonical FFS testing doctrine. No flags. Reference skill: `/feature-spec`, `/spec-decompose`, `/feature-implement`, `/code-uplift` and `/fix` load it when they write or judge tests. Invoke it directly to ask how something should be mocked or what the browser gate requires. Rules: mock-minimization ladder (real thing, own adapter, network boundary only, jsdom for logic not UI truth); fail-closed real-browser gate (`scripts/gsd/canary-gate.sh`: `status=="passed"`, zero console errors, zero network failures, fresh against HEAD); the agent that wrote the code does not author its acceptance tests; BDD scenarios only for user-facing stories; 80% coverage floor; post-deploy smoke of 60s or less |

## Code Review + Quality

| Command | What it does |
|---------|-------------|
| `/review` | Pre-landing code review: checklist pass + specialist army + adversarial review |
| `/plan-ceo-review` | Strategy review of a plan file (premises, scope, alternatives) |
| `/plan-eng-review` | Architecture review (coupling, test gaps, performance, security) |
| `/plan-design-review` | UI/UX review (7 dimensions, interaction states, responsive) |
| `/plan-devex-review` | Developer experience review (TTHW, error messages, CLI ergonomics) |
| `/codex` | Run OpenAI Codex as outside voice for adversarial second opinion |
| `/review-gate` | Cross-model review on the staged diff: up to three passes (general, adversarial, test-coverage gap; a `light` tier runs only the first) plus an honest-verifier pass when a spec resolves. Canonical gate used by the `/feature-implement` finish tail (Step 6), and so by `/fix`, before `/ship`. The opposite host's CLI reviews first; one bounded active-host fallback is reported as `DEGRADED`; with no reviewer the gate fails closed. Prints `GATE: PASS` (exit 0) or `GATE: FAIL` (exit 1: any CRITICAL or HIGH finding, or a failed or abstained verifier). Flags: `--all` (changes vs main), `--file PATH`, `--dry-run`; `REVIEW_TIER=light\|standard\|full` overrides tier detection. At the gsd ship seam (`scripts/gsd/review-gate-command.sh`) the same outcome is JSON: `"verdict":"APPROVED"` or `"verdict":"REVISE"` with a `BLOCKED:` note. |
| `/verify-review` | Verify a review before acting on it. No flags. Use when a verdict arrives (`/review-gate`, `codex review`, a PR review, a sub-agent critic) and you are about to merge, fix or push back. Open the load-bearing claims at current HEAD, pin the ref the review measured, scan for commits newer than its evidence, then classify each claim: `REAL_BLOCKER`, `REAL_NON_BLOCKING`, `STALE`, `WRONG`, `CONFIRMED_PASS` or `UNMEASURED`. Never forward a reviewer's claims to a fixer unverified. |
| `/code-uplift <path\|module>` | Review, refactor and test-uplift code that is already written; findings-driven rather than spec-driven. Not for new features (`/feature-spec`) or one named bug (`/fix`). Runs a cross-model review sweep into `specs/NNN-uplift-<slug>/REVIEW.md`, seeds a gsd project with phases fix-critical → refactor → test-uplift → e2e-smoke, executes like `/feature-implement`, then the finish tail. Flags: `--review-only` (findings ledger only), `--no-refactor` (test uplift only), `--coverage-floor N` (default 80), `--autonomous`, `--slop-only <diff-base>` (green-baseline-walled, deletion-first cleanup scoped to `git diff <diff-base>`), `--no-finish` |

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
| `/fix "bug description"` | Investigate (root cause, not symptom) then fix via `/feature-implement --adhoc`. Non-interactive by default; only the review-gate step crosses to the opposite model family. |
| `/fix "desc" --interactive` | Restore manual gates between phases (default is non-interactive) |
| `/fix "desc" --autonomous` | Pass through to `/feature-implement --adhoc --autonomous` (fail-closed walls: fresh preflight + grant ledger required) |
| `/fix "desc" --no-finish` | Pass through — skip the finish tail (review-gate/ship). Operator-accepted-risk. Replaces the retired v3.x `--no-review-gate`/`--no-audit` |

## Debugging

| Command | What it does |
|---------|-------------|
| `/investigate` | 5 Whys root cause analysis, scope-locked to affected module |
| `/browse` | Open `$B` (gstack browser) for manual inspection |

## Project Management

| Command | What it does |
|---------|-------------|
| `/spec-status [NNN] [--continue-compact] [--no-handoff]` | Read-only "where are we?" status check for a spec run — fans out over git, `.planning/`, gates ledger, runner state, evidence, and hygiene; writes a status report + `/handoff` (chains `/continue-compact` instead with that flag). `--no-handoff` skips the handoff. Facts include a read-only `== CLOUD ==` section, printed only when a `/swarm --cloud` run ledger exists. There is no `--cloud` flag. The status report and handoff are committed only through `scripts/gsd/publish-scanned-handoff.sh`, which scans first and blocks on a finding |
| `/spec-guide [NNN] [--no-live] [--output PATH]` | Produce developer, admin, and user instructions for a spec, map every step to its real browser/API/MCP/chat/email/CLI/webhook/worker vehicle, execute those proofs, and write `<spec-dir>/usage-guide.md`; design changes require `/design-review` |
| `/continue-compact` | Save a durable handoff and produce the exact host-native resume command before the built-in `/compact`; Codex invokes the skill as `$continue-compact` |
| `/retro` | Weekly retrospective of what shipped, what broke, what to improve |
| `/checkpoint` | Save progress mid-session for resume later |
| `/health` | Codebase quality check (dead code, test coverage, lint) |
| `/goal-wrap [--gates] "objective"` | Bundle current work into a self-contained, anti-drift `/goal "..."` prompt with tracked DONE WHEN proof commands. Use before `/clear`, agent handoff, or switching machines. `--gates` reverts to ask-first behavior (default: full autonomy — commits/push/merge/deploy pre-approved). Degrades gracefully without repowise/gbrain/`/prompt-master`/`/handoff` — see the skill's own "Soft dependencies" table. |
| `/area-status <area> [--live]` | Is a named area of the codebase actually done, evidenced and safe to call green. Stage 1 runs `skills/area-status/scripts/collect-area-facts.sh` (measured ref, behind-count, spec estate, code surface, test inventory, newest evidence, post-evidence commits, verdict hint). Stage 2 fans out read-only volume, execution and judgment roles. Writes a graded report: header with the measured ref (and a DRIFT flag when behind), per-surface verdict `GREEN`, `RED` or `UNMEASURED` (commits newer than the newest evidence, or no evidence, force `UNMEASURED`), claims graded `CONFIRMED` or `INFERRED`, and a `## Corrections` section. Read-only toward the area. `--live` allows outward probes, each preceded by `check-grant "$RUN_ID" --action "probe:<target>"`; a refused probe is skipped and noted, never fatal. Area tokens use `[A-Za-z0-9._-]` only |
| `/adopt-wip` | Salvage coherent uncommitted work left in a stalled worktree or a dead agent session. No flags. First confirm the worktree is stale (file mtimes, processes, and `bash scripts/gsd/liveness-check.sh <pidfile> <state-dir>`: exit 0 ALIVE means wait, exit 1 DEAD means safe). Then read the whole diff, check coherence, exercise the load-bearing claim, and decide adopt, rebuild or wait. Adopted code gets no gate discount: it needs its RED proof and the full gate chain including `/review-gate`. Credit the source worktree in the commit body |
| `/retro-triage` | **Maintainer-only.** Renders an advisory brief from validated FFS retro issues for a maintainer-reviewed `/feature-spec`. No flags; one fixed command block. It runs only when the local origin is the canonical upstream repo and the server-side push-permission check passes, then reads open issues labelled `source/ffs-retro`. Issue titles, prose and labels are untrusted; only revalidated fingerprint, priority and occurrence counts are rendered. Output: `RETRO-TRIAGE:ready` with one cluster per fingerprint group, or `RETRO-TRIAGE:no-issues`. Errors: `RETRO-TRIAGE:wrong-origin`, `RETRO-TRIAGE:not-maintainer`, `RETRO-TRIAGE:transport-error`. See [Retro](retro.md) |

## Environment and CI setup

| Command | What it does |
|---------|-------------|
| `/ffs-init [--detect-only\|--answers <file>\|--yes\|--update\|--check\|--force\|--reset-declines\|--skip-deps]` | One batched session. Phase 0 checks repo-scoped dependencies with `scripts/gsd/deps.sh check` (`--skip-deps` skips it; `--detect-only` and `--check` never run it). Then it fills in `config/environments.yaml`: environments, test tiers, and (behind its own gate) surfaces. Decides and writes nothing itself — every write routes through `env-registry.sh apply`. Secrets by NAME only. In an interactive session, when no consent decision is recorded, it also asks whether to enable public retro diagnostics and records the answer with `scripts/gsd/retro.sh consent --grant` or `--revoke` (arguments of `retro.sh`, not `/ffs-init` flags); non-interactive modes never ask or grant. See [Environment registry](environment-registry.md) and [Retro](retro.md) |
| `scripts/gsd/env-registry.sh <detect\|check\|render\|apply\|seed>` | The deterministic owner of the registry: propose rows, validate (schema + leak scan + referential integrity), render `templates/ci/` as `ffs-*.yml` proposals, write atomically, seed preflight rows. Exit 0 ok / 1 schema-usage-refusal / 2 leak |
| `scripts/gsd/test-tier.sh <fast\|full\|nightly\|live>` | The only source of CI test commands. stdout = registered command lines; exit 2 unknown tier, 3 registry unreadable — a lookup failure fails distinctly from a test failure. See [CI templates and test tiers](ci-templates-and-tiers.md) |
| `scripts/gsd/promote-emit.sh <run_id>` | Emits — never runs — the staging→prod promote line for each staging grant on a run, bound to `_promotions` records only |

## Repository Hygiene

| Command | What it does |
|---------|-------------|
| `/git-branch-consolidate` | Read-only audit of every branch and worktree — is the content already landed, what does it still owe, does it have tests, is its spec/plan/tasks trail complete. Emits an ordered merge set, cleanup set, and testing gaps to `.planning/ESTATE-<UTC>.md`; never checks out, merges, or deletes. `--autonomous` (Step 4-A) stays report-only by default: it prints the deletion plan only when all four proofs hold — a queue-derived exact-scope `consolidate:estate` grant, fresh estate evidence with `landed==true` for every target, exact target-set tuple proof, and a green `assert-merged` result per target PR — and deletes nothing. Explicit `--execute` requires the same four proofs, rechecks grant scope/expiry and target OIDs immediately before each effect, and delegates every deletion solely to `scripts/gsd/run-finalizer.sh` under its merged-head proof — there is no other deletion path |
| `/git-branch-cleanup` | Triage branches against the base branch, merge only CI-green non-gated PRs, prune merged refs + worktrees. Squash-merge and operator-gate aware; owns the deletion approval flow |
| `/land-queue` (`bash scripts/gsd/land-queue.sh [--repo DIR] [--base NAME] [--run-id ID] [--posture zero\|floor] [BRANCH...]`) | Serially land finished branches: collect from takeover records, estate discovery and explicit branch arguments; rebase, implement, review, watch CI, merge under an exact `merge:pr-N` grant, finalize, and journal every step. Strictly serial; limits are 90 minutes per item, 8 hours and 10 items per queue. `--drain` asks a running queue to finish at the next item boundary. `--resume QUEUE-ID` re-enters a crashed queue from its journal. `touch <store>/land-queue/STOP` aborts before the next effect. `--parallel` is refused (`PARALLEL-UNSUPPORTED:v1-serial-only`). Item terminals: `LANDED`, `BLOCKED:<reason>`, `SKIPPED:<reason>`; each non-`LANDED` item prints a one-command `HUMAN-INBOX:` unblock. `--posture floor` blocks an item with no opposite-vendor reviewer; `zero` (default) continues but records the degradation with `gates.py note-degraded` |

**Pair order:** `/git-branch-consolidate` audits and decides, then hands its
`delete-safe` set to `/git-branch-cleanup` to execute. Land before cleanup, or
you delete the only copy. In `--autonomous` mode this handoff does not apply:
deletion is delegated solely to `run-finalizer.sh` — no second deletion owner.

## Environment Variables

| Var | Default | Effect |
|-----|---------|--------|
| `--max-retries` (flag, `scripts/ralph-retry.sh`) | `3` | Max retry attempts per phase on QA failure — a script flag, not an env var |
| `RALPH_AUTO_QA` | `1` | Set to `0` to disable the PostToolUse auto-qa hook (`scripts/hooks/post-implement-batch.sh`) |
| `RALPH_DEBOUNCE_SECS` | `30` | Quiet window before auto-qa fires |

## gsd-core (Orchestration)

The gsd loop replaced ruflo as FFS's orchestrator (spec 002). Pin: `@opengsd/gsd-core@1.15.0`.
Use `node node_modules/.bin/gsd-tools` — bare `npx gsd` resolves to the WRONG package.

| Scenario | Use |
|----------|-----|
| Seed a project from a spec | `/spec-decompose NNN` (writes `.planning/`, drives `/gsd-plan-phase`) |
| Execute the current phase | `/feature-implement NNN` → `/gsd-execute-phase N` |
| Unattended run | `/feature-implement NNN --autonomous` (preflight PASS + grant ledger required) |
| Headless drive | `TIMEOUT=3600 bash scripts/gsd/gsd-run.sh /gsd-<cmd> ...` (trimmed-MCP, auth-scrubbed) |
| Managed run-state (opt-in, experimental) | `FFS_MANAGED_INGRESS=1` + `bash scripts/gsd/ffs-frontend.sh <frontend> ...`; `gsd-run.sh` then refuses with exit 78. Env vars and refusal codes: [Configuration](configuration.md#managed-run-state-opt-in-spec-014-release-b) |
| Quick single-task fix | `/fix` → `/gsd-quick` |
| Resume after context reset | `/gsd-resume-work` (STATE.md is the resume point) |
| Verifier found gaps | `/gsd-plan-phase N --gaps` → `/gsd-execute-phase N --gaps-only` |

First, `scripts/gsd/planning-scope.sh NNN` picks the gsd project the spec plans
under. A spec with its own `.planning/<NNN>/ROADMAP.md` uses that project, and
every later step reads its ROADMAP, config and phases instead of the root
`.planning/`. Start an interactive session for such a spec with the project set
(`GSD_PROJECT=<NNN> claude` or `GSD_PROJECT=<NNN> codex`); without it,
`/feature-implement` stops before execution because the gsd-tools calls it
spawns would read the root project. `--autonomous` passes `GSD_PROJECT` to the
headless runner itself.

Before phase execution, `scripts/gsd/requirement-ownership-gate.sh N` requires
the phase's PLAN frontmatter to own every ROADMAP requirement exactly once.
Preparatory plans use `requirements: []`; an ID goes only on the final plan that
genuinely completes it. Headless execution enforces this before any model probe.

Next, `scripts/gsd/plan-probe-gate.sh N` re-runs gsd-core's deterministic plan
probes (`check verify-command-paths`, `check verify-failure-directions`). Every
runnable `<automated>` needs its own `<fails_when>` naming the observable failure
signal, and every command target must resolve. It honors `GSD_PROJECT`, and
headless execution enforces it too.

Also before execution — after the ownership gate, before any task starts —
`scripts/gsd/plan-wall.sh` reviews the phase's plan(s) with a model distinct
from the one that wrote it (producer≠reviewer, always on; see
[Model tiers](model-tiers.md#the-plan-wall)). An unresolved CRITICAL finding blocks and buys one repair round. HIGH-only findings pass as `PASS-RESIDUAL` and are checked again at the executed-diff review. `PLAN_WALL=off` skips only with a recorded waiver.

Completion authority is gates.py, never gsd self-report: `workflow.test_command` =
`scripts/gsd/gates-test-command.sh` (run-gate + strict verify-done + plan-wall
completion backstop — a phase can start unwalled but cannot COMPLETE without a
passing wall record),
`workflow.code_review_command` = `scripts/gsd/review-gate-command.sh` (grant wall + codex),
and the `gsd-phase-evidence-gate.sh` PreToolUse hook blocks ROADMAP/STATE phase flips
without evidence.

Do not use provider-key execution paths for feature tasks. Codex sessions execute
through `codex exec`; Claude sessions execute through `claude -p`.

## Reference

- [TDD & BDD Guide](tdd-bdd-guide.md) — Research-backed best practices (Fowler + MSR 2026), anti-patterns, agent over-mocking warning, Gherkin rules, test pyramid
- [Pipeline overview](pipeline.md) — Full pipeline diagram with QA Ralph loop
- [QA Ralph Loop](qa-ralph-loop.md) — Per-phase QA architecture and configuration
- [Dependencies and integrations](dependencies.md) — Every integrated system, who owns it, and how it is pinned
- [Environment registry](environment-registry.md) — Environments, test tiers, and surfaces in one committed file
- [CI templates and test tiers](ci-templates-and-tiers.md) — Workflow templates, the render anti-clobber rule, tier lookup
- [Cross-session messaging](cross-session-messaging.md) — How concurrent sessions coordinate and hand off

## Machine gates (lib/gates.py)

Completion authority for autonomous runs — evidence, not agent self-report.

| Command | What it does |
|---------|-------------|
| `gates.py record-gate T042 --exit N --cmd ... --before ... --after ...` | Record a task's test-gate outcome in the evidence store (`$GATES_STORE`, default `.feature-fix-swarm/evidence.json`) |
| `gates.py verify-done T042` | Exit 0 iff passing gate evidence exists — the ONLY thing that legalizes an `[X]` flip. Prints `executed_by` |
| `gates.py verify-done T042 --strict` | (or `GATES_STRICT=1`) additionally reject caller-recorded evidence — only `run-gate` runner evidence passes. The loop runs strict (v3.14.0) |
| `gates.py run-gate T042 [--artifact name@sha256:<64hex>] [--timeout S] -- CMD…` | PREFERRED: execute the gate and record the REAL exit code — evidence bound to the runner. `--artifact` binds a staging gate to an artifact identity for later promote checks. Default timeout 1800s; override with `--timeout <seconds>` or `GATES_RUN_TIMEOUT` (flag wins, cap 86400s); a timeout records `exit_code: 124` instead of crashing |
| `gates.py run-red T041 -- CMD…` | PREFERRED: execute the RED test; proof stored only if it really failed |
| `gates.py record-red T041 --exit N < log` | Store a RED proof; rejected unless the log shows a real failure. Prints a forgeability WARNING |
| `gates.py check-red T041` | Exit 0 iff RED proven — blocks the paired GREEN task until then |
| `gates.py phase-score T040 T041 …` | Truth score from stored evidence (compile .35 / tests .25 / lint .20 / typecheck .20, normalized over categories present; missing evidence → 0.0). Exit 1 below 0.95 → phase rollback (v3.14.0) |
| `gates.py note-failure T042 --sig "…"` | Record a failure signature; exit 1 when the same signature repeats twice in a row → STOP, no-progress (v3.14.0) |
| `gates.py proof RUN T040 T041 … [--defer "name: reason"]… [--strict] [--out path]` | Emit the per-run proof artifact (`proof-<run>.json`): one claim per task with evidence cmd, real exit, sha256 of stored log material, live-vs-structural kind; named deferrals; go/no-go verdict. Exit 1 on no-go (v3.15.0) |
| `gates.py scan-tamper < diff` | Flag reward-hacking moves: deleted asserts, added skips, `exit 0`, CI edits. Exit 1 on findings |
| `gates.py analyze spec.md tasks.md` | Spec↔tasks coherence gate (spec-kit analyze analog); `/feature-implement` refuses to start on findings |
| `gates.py findings-queue add ... [--severity S] [--source wall\|review-gate] [--plan PATH] [--spec ID] [--security] [--needs-requirement]` | Record a review finding (plan-wall, review-gate, or manual); re-adding a RESOLVED signature reopens it. `--spec` binds it to a spec; `--security` marks it security-class; `--needs-requirement` marks a finding whose fix needs a new requirement (both flags are sticky) |
| `gates.py findings-queue resolve ... --disposition refute\|fix\|waive\|issue\|descope --reason "…"` | Resolve a finding; disposition and reason are required and kept in history. `issue` and `descope` are operator choices (agent tool calls are blocked by the `operator-only-guard` hook), refused for security HIGH/CRITICAL findings. `fix` is refused on a non-security needs-requirement finding |
| `gates.py findings-queue retag <sig> --in-spec` | Operator-only (agent tool calls blocked by the `operator-only-guard` hook), after amending the spec: clears the needs-requirement flag so the finding can close as `fix` |
| `gates.py findings-queue list [--unresolved] [--severity ...] [--source ...] [--plan ...]` | Filtered finding listing; the plan wall reads `list --unresolved --source wall --severity HIGH,CRITICAL --plan <plan>`; only an unresolved CRITICAL in that list blocks |
| `gates.py spec-id` | Prints the spec identity (`spec-NNN`). Order: `FFS_SPEC_ID` env, then branch `NNN` prefix, then `GSD_RUN_ID` (stderr `SPEC-ID:unbound`). `FFS_SPEC_ID` disagreeing with the branch `NNN` exits non-zero (`SPEC-ID:mismatch`) |
| `gates.py loop-round <id> <loop> --durable --max N --site skill\|seam [--token T]` | Durable review meter (one `review:spec` per spec, max 2). `--site` without `--token` derives the token from the production-classified candidate (merge-base `${FFS_BASE:-<default branch>}` to working tree; tests, docs, specs, `.planning` excluded). A token spent at one site is free once at the other. Also `--reset` (one loop), `--reset-all` and `--note-count N` (record the current round's new-finding count) |
| `gates.py intent-check --spec specs/NNN-*/spec.md --stage plan\|finish [--repin]` | Checks the spec's Intent block. `plan` validates the block and pins its hash on first run; a later change fails `INTENT-FAIL`. `finish` measures the candidate's production line delta (merge-base of `${FFS_BASE:-<default branch>}` to the working tree; tests, docs, `specs/` and `.planning/` excluded) against the block. Exit 0 ok or `INTENT:absent` (warning), 1 `INTENT-FAIL`, 2 usage. `--repin` is operator-only (agent tool calls blocked by the `operator-only-guard` hook) and plan-stage only: it pins an edited block as a new revision only when the edit narrows the intent (sign no looser, budget no higher), and never changes review budgets |
| `gates.py grant RUN --action type:target [--action …] [--ttl-hours H] [--reason "…"]` | Operator pre-approval ledger. Actions must be typed `type:target`. Default TTL 72h, cap 168h. `consolidate:*` actions are refused here; only `grant-consolidate` mints them |
| `gates.py check-grant RUN --action type:target [--artifact name@sha256:<64hex>] [--manifest PATH] [--require-environments]` | Exit 0 iff the action is granted and unexpired for that run. `deploy:`, `flip:` and `migrate:` `prod-*` actions also need a fresh staging→prod promote record for the artifact; `--require-environments` (or `FFS_ENV_REGISTRY_REQUIRED=1`) additionally needs a committed `ffs.environments/v1` registry. `hotfix:prod-*` is the emergency escape and needs an operator grant with `--reason` |
| `gates.py pending RUN [--action type:target --reason "…"]` | With `--action`, record an unlisted gate as pending. Without it, list pending actions; exit 1 if any |
| `gates.py promote RUN --from ENV --to ENV --surface S --artifact name@sha256:<64hex> --evidence ID [--evidence ID…] [--ttl-hours H]` | Record promote proof that binds an artifact identity to recorded staging gate evidence |
| `gates.py grant-consolidate RUN --queue-id ID --journal-store DIR [--ttl-hours H]` | Mint the queue-derived `consolidate:estate` grant from a terminal land-queue journal. stdin is never read and the TTL is clipped to the queue deadline. Internal; called by `scripts/gsd/land-queue.sh` |
| `gates.py preflight MANIFEST --run RUN` | Run the env and probe checks in a preflight manifest and record the result. Exit 1 on any failure |
| `gates.py check-preflight RUN` | Exit 0 iff a fresh recorded preflight PASS exists |
| `gates.py note-refuted T042 --reason "…"` | Close a task with zero diff because its diagnosis was refuted; the reason is required. Route the refutation through `/review-gate` before flipping the checkbox |
| `gates.py confirm-refuted T042` | After the refutation survived review-gate refute-or-promote, unlock strict `verify-done`. Trusted caller; prints a warning |
| `gates.py delegation-audit [FILE] [--threshold N] [--advisor-cap N]` | Count sub-agent spawns by model in a transcript (file or stdin); report unpinned build spawns and inline mechanical work. Advisory only; always exits 0 |
| `gates.py store-dir` / `gates.py store-path` | Print the resolved evidence-store directory / file. They run before any ledger read so a caller can name the ledger first. Internal; called by `scripts/gsd/land-queue.sh`, `scripts/gsd/takeover-record.py`, `scripts/gsd/takeover-check.sh` and the `/spec-status` facts collector |
| `gates.py takeover-state RUN` | Print the run's takeover state as JSON. Read-only. Internal; called by `scripts/gsd/takeover-record.py` and `scripts/gsd/takeover-check.sh` |
| `gates.py takeover-expectation RUN` | Print `{"takeover_expected": true\|false}` for the run. Read-only. Internal; called by `scripts/gsd/takeover-check.sh` |
| `gates.py takeover-expect RUN [--created-at N] [--dirty-digest <64hex>]` | Record that a takeover is expected for the run; prints `TAKEOVER-EXPECTED`. Internal; called by `scripts/gsd/takeover-record.py` |
| `gates.py takeover-evaluate RUN --snapshot-fd FD [--action A]…` | Print the takeover authority view for the given actions as JSON, read from an open snapshot descriptor. Internal; called by `scripts/gsd/takeover-check.sh` |
| `gates.py takeover-consume RUN --consumed-at N --store-dir-fd FD [--store-fd FD] --takeover-dir-fd FD --record-fd FD --record-name NAME --snapshot-sha256 SHA [--deadline-ms N]` | Mark a takeover record consumed under the evidence lock. Prints `REFUSED:<reason>` and exits 1 when the record does not match. Internal; called by `scripts/gsd/takeover-check.sh` |
| `--store-dir-fd FD --store-fd FD` | Descriptor-pinned, read-only store flags. Accepted only by `takeover-state`, `takeover-expectation`, `check-preflight` and `check-grant`; any other command exits 2 with `TAKEOVER-FD-FLAGS-REJECTED` |
| `gates.py waiver --run-id R --gate G --env-var V` | Append one auditable operator waiver (a bypass event) to the store; prints `WAIVER-RECORDED`. Internal; called by `scripts/gsd/waiver-record.sh` |
| `gates.py read-posture RUN` | Print the run's durable autonomy posture as `<zero\|floor> <source>`; exit 1 with `POSTURE-ABSENT` if none. Internal; called by `scripts/gsd/land-queue.sh` |
| `gates.py note-posture RUN --posture zero\|floor --source S` | Record the run's posture and provenance. Immutable per run: a conflicting write exits 1. Internal; called by `scripts/gsd/land-queue.sh` |
| `gates.py note-degraded rung-attempt --rung-id ID --outcome O` / `note-degraded invocation --run-id R --seam S --degraded true\|false --invocation-id ID [--branch B --head H --baseline B --repo R --changed-file F… --production-file F…]` | Record a review-ladder rung attempt or a review invocation, degraded or not. Also `--tripped` (list tripped rungs), `--probe-check RUNG` (`PROBE-DUE` or `PROBE-NOT-DUE`) and `--reset-rung RUNG`. Internal; called by `scripts/gsd/adversary-host.sh` and the land-queue review step |
| `gates.py canary-evidence --run-id R --sha S --pass true\|false [--created-at T] [--ended-at T]` | Record post-deploy canary evidence for a commit. Internal; called by `scripts/gsd/canary-gate.sh` and `scripts/gsd/canary-deploy-gate.sh` |
| `gates.py rollback-dryrun --run-id R --surface S --command C --exit-code N [--artifact-sha SHA]` | Record the result of a rollback dry run. No script in this repo calls it; `scripts/gsd/digest.sh` reads the recorded rows |
| `gates.py map-run --ledger-run-id L --runstore-id R` / `map-run --get --ledger-run-id L` | Record, or read back, the mapping from a ledger run id to a run-store id. Internal; called by `scripts/gsd/gsd-run.sh` |

### Budget exhaustion

Review budgets are never renewed. `review:spec` and the plan wall's `review:wall:<phase>` are keyed on the spec, so a new drive or run id gets no fresh allowance. When a `review:*` budget is spent, gates.py prints the contract below and records it under `_exhausted[spec-NNN]`:

```text
BUDGET-EXHAUSTED <loop> spec-NNN: stopped. Operator picks one: merge-with-issues | descope | stop. Recommended: <amend spec | follow-up spec> — run /prompt-master (Codex: $prompt-master) with the prompt below.
```

The line is followed by a ready prompt. The run's only automatic action is stop. An exhausted spec never reaches APPROVED at the seam. `merge-with-issues` means the operator resolves each open finding as `issue` and merges by hand.

Gate ladder (cheap→expensive, fail-fast): compile → typecheck → lint → unit → integration → e2e → LLM review. Truth score is computed by `phase-score` at every phase gate; < 0.95 after max retries → rollback to phase checkpoint. No-progress is enforced by `note-failure` in the retry loop. LLM review rounds capped at 2 per spec (`review:spec`). `record-gate` still exists for humans but warns at runtime and is rejected under strict mode. Optional PreToolUse hook `hooks/tdd-gate.sh` blocks source writes with no matching test file.

### Intent block

`/feature-spec` (Codex: `$feature-spec`) and the `plan-decompose` synthetic spec write an Intent block in `spec.md`:

```
## Intent
<!-- ffs:intent -->
prod_delta: negative
prod_budget_lines: 0
delete_targets: scripts/old-tool.sh
inherit: specs/100-parent/spec.md
```

- `prod_delta`: expected sign of the production line delta: `negative`, `neutral` or `positive`. Skill and prompt text count as production.
- `prod_budget_lines`: `negative` needs net < 0; `neutral` needs |net| <= budget; `positive` needs net <= budget.
- `delete_targets`: for a `negative` spec, each path must be gone or net-negative at finish.
- `inherit`: a member spec may not loosen the parent's sign or raise its budget.

The plan wall checks and pins the block at admission. `/review-gate` (Codex: `$review-gate`) and the ship seam check it at finish before any review round is spent. A breach is `INTENT-FAIL`: delete more, or stop. A spec without the block passes with `INTENT:absent` (a warning).
