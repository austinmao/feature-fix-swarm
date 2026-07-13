# feature-fix-swarm

QA-per-phase enforcement for Claude Code and Codex harnesses, with cross-model adversarial completion audit.

Stop bugs from compounding. Test every phase before the next one starts. Fix bugs with an automated investigate-fix-verify loop. Every completion is gated by a hostile cross-model audit (Codex GPT-5) that tries to prove the work is NOT done. The shared workflow is host-neutral; Claude and Codex are runtime adapters over the same task state.

## v3.0 — Native /goal integration

Claude Code 2.1.139+ ships native `/goal` ([docs](https://code.claude.com/docs/en/goal)) which owns the continuation loop. v3.0 strips our Stop hook + marker file + continuation tracking — ~300 lines of dead code gone. We keep what native /goal doesn't do: cross-model adversarial audit + `/review-gate`.

**New /feature pipeline:**

```
Operator: /goal "spec NNN done: every phase audit verdict=pass, review-gate PASS, canary 200"
Skill:    /autoplan → /spec-decompose
          foreach wedge:
            /feature-implement <wedge>
            run-state audit --kind phase    → writes verdict to audits.jsonl
          /qa
          /review-gate                      → cross-model review of full branch diff
          /ship + /canary
Native /goal: condition holds → auto-clears. Done.
```

**New /fix pipeline:**

```
Operator: /goal "bug fixed: latest run-state audit --kind fix verdict=pass AND qa green"
Skill:    investigate → implement → qa-only → qa → /review-gate
          run-state audit --kind fix → writes verdict
Native /goal: condition met → clears.
```

After `bash setup.sh`:

```bash
~/.claude/bin/run-state list                  # show all runs
~/.claude/bin/run-state status <run_id>       # detailed status
~/.claude/bin/run-state audit <run_id> --kind <fix|feature|phase> --context K=V ...
~/.claude/bin/run-state abort <run_id>        # kill a stuck run
```

Full details: [lib/run_state/README.md](lib/run_state/README.md). Native /goal docs: https://code.claude.com/docs/en/goal.

**Upgrading from v2.x?** Setup.sh no longer registers Stop / SessionStart hooks. To remove leftover v2.x hooks from `~/.claude/settings.json` manually, edit the file (or use `jq` to filter out entries with commands containing `run-state-stop` and `run-state-session`).

## The problem

AI agent harnesses (Claude Code skills, LangGraph workflows, custom pipelines) build features by decomposing specs into tasks and executing them sequentially. The typical pattern:

1. Decompose a feature into 20-50 tasks
2. Execute all tasks
3. Run QA at the end
4. Discover that task 3 broke something and tasks 4-50 built on top of it

By the time you find the bug, 47 tasks of context separate you from the root cause. The fix is expensive, the debugging is painful, and the LLM has lost the thread.

**feature-fix-swarm solves this with two disciplines:**

- **Build mode** (`/feature-implement --qa-loop`): After every phase of tasks completes, a QA swarm runs deterministic tests + LLM review agents. Bugs are caught at the phase boundary, not at the end. Failures trigger an automatic investigate-fix-retest loop (max 3 retries) before the next phase starts. No phase advances on red.

- **Fix mode** (`/fix "bug description"`): Post-ship remediation. Takes a bug report, searches prior fix patterns (learning system), investigates root cause with 5 Whys, coordinates fix agents through Ruflo, executes them through the active host CLI, verifies with focused QA, then runs full regression QA. Loops until green.

## OSS contract

The shared package contract is the same on every host:

1. spec
2. plan
3. autoplan
4. spec-decompose
5. feature-implement
6. qa
7. fix
8. ship
9. canary
10. goal-wrap when the session should hand off instead of continuing in-place

The core workflow should not encode Claude-only or Codex-only behavior. Host-specific logic belongs in a thin adapter layer that renders the shared task graph for the active runtime.

## Normalized task graph

feature-fix-swarm treats `tasks.md` as a normalized intermediate representation, not just a text checklist.

Shared task fields:
- `model_tier`
- `agent_role`
- `qa_lanes`
- `host`
- `executor`
- `isolation_state`

Rationale:
- `model_tier` lets the same task graph render different model ladders for Claude and Codex.
- `agent_role` gives Ruflo a stable routing vocabulary from the exact hybrid catalog instead of free-form names.
- `qa_lanes` makes OpenClaw and Telegram lanes explicit rather than implied.
- `host` and `executor` preserve runtime intent for resume and logging.
- `isolation_state` keeps branch and worktree handling deterministic.

The implementation layer renders those fields for the current host, but the task graph itself stays host-neutral.

## Host-aware routing

- Claude Code uses `fable` for orchestration/planning, `opus` for architecture
  and verification, `sonnet` for main development, and `haiku` for mechanical
  work.
- Codex uses the researched equivalent ladder from
  `scripts/gsd/model-equivalents.sh`: Fable/Opus → `gpt-5.6-sol`, Sonnet →
  `gpt-5.6-terra`, Haiku → `gpt-5.6-luna`.
- GSD is the coordination engine. Its Claude and Codex runtime surfaces are
  installed separately; explicit Claude aliases in generated Codex agent TOMLs
  are materialized by `scripts/gsd/codex-model-sync.sh`.
- A fixed read-only capability probe prefers the invoking host for ordinary
  GSD execution (`codex exec` from Codex, `claude -p` from Claude). If that
  CLI/model/quota is unavailable, the other host may be selected before launch.
  A started or timed-out stateful drive is never replayed across vendors; its
  bounded failure includes host-specific resume guidance.
- `/review-gate` deliberately tries the opposite host first: Codex Sol/xhigh
  for Claude-produced work, Claude Opus for Codex-produced work. Read-only
  reviews may make one bounded active-host fallback. The degradation is
  explicit, and mandatory review fails closed if neither host returns evidence.
  Set `FFS_CROSS_VENDOR_FALLBACK=off` only for forensic isolation.

## How it works

```
Phase N tasks complete
        |
        v
+-- Deterministic hooks ------+
|  vitest run (TS/JS files)    |    $0 cost
|  pytest -x (Python files)    |    deterministic
+----------+-------------------+
           |
           v
+-- LLM QA swarm (ruflo) -----+
|  qa-e2e (browser via $B)     |    ~$0.05 each
|  qa-review (code review)     |    parallel
|  qa-security (OWASP scan)    |    via ruflo
+----------+-------------------+
           |
      pass? --yes--> Phase N+1
           |
          no
           |
           v
+-- Investigate + fix ---------+
|  /investigate (5 Whys)        |    scope-locked
|  fix sub-agent                |    TDD: test first
|  /qa-only (re-verify area)    |    targeted
+----------+-------------------+
           |
      retry < 3? --yes--> Re-run QA swarm
           |
          no
           |
           v
      Mark [F], stop pipeline
      Print artifacts + resume command
```

## Prerequisites

### Required

- At least one supported host: **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)**
  or **[Codex CLI](https://github.com/openai/codex)**. Install both to enable
  the opposite-host review gate in either direction.

- **[gstack](https://github.com/garryslist/gstack)** by Garry Tan
  Provides the skills that feature-fix-swarm orchestrates:
  - `/investigate` -- 5 Whys root cause analysis
  - `/qa` -- full browser-based QA testing
  - `/qa-only` -- report-only QA (no fixes)
  - `/review` -- pre-landing code review with specialist army
  - `/ship` -- automated PR creation with tests + review

  Install gstack:
  ```bash
  cd ~/.claude/skills && git clone https://github.com/garryslist/gstack.git
  ```

- **[ECC](https://github.com/affaan-m/ECC)** by affaan-m and **[wshobson/agents](https://github.com/wshobson/agents)** by wshobson
  Provide the exact-agent catalog used by the decomposition and execution skills. `bash setup.sh`
  checks both upstream repos against their current `main` HEAD, installs them if they are missing,
  and refreshes them when the recorded commit drifts from upstream.

- **[Open GSD Core](https://github.com/open-gsd/gsd-core)** — exact-pinned by
  this package and installed by `setup.sh` for every available host runtime.
  GSD owns plan/execute/verify orchestration; Ruflo is retired.

- **[Spec Kit](https://github.com/github/spec-kit)** by GitHub
  Provides the spec/plan/tasks bootstrap (`/speckit.specify`, `/speckit.plan`, `/speckit.tasks`) that feature-fix-swarm builds on.
  Spec Kit is installed via `uv`:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  uv tool install specify-cli --from git+https://github.com/github/spec-kit.git
  specify version
  ```

- **[goal-wrap](https://github.com/austinmao/goal-wrap)** by austinmao
  Provides the handoff/continuation layer for `/goal-wrap`.
  It depends on the `prompt-master` and `handoff` skills.
  Install it with the companion skills:
  ```bash
  npx skills add austinmao/goal-wrap --skill goal-wrap --skill handoff -g -a claude-code -a codex -y
  npx skills add nidhinjs/prompt-master --skill prompt-master -g -a claude-code -a codex -y
  ```

  If either skill is missing, `bash setup.sh` will prompt to install the full prerequisite set.

### Opposite-host review runtime

- Install the other host CLI to enable cross-vendor adversarial review in
  `/review-gate` and GSD's ship review seam.

  The review gate is part of the default finish tail for `/feature-implement`
  and `/fix`; `--no-finish` is the explicit accepted-risk opt-out. Direct
  When the opposite CLI is unavailable, review tries the active host once and
  labels the result `DEGRADED`; both unavailable, an unparseable reviewer, or a
  ship reviewer without exactly one line-anchored verdict returns `REVISE`.
  No mandatory ship review fail-opens. ~$2 + ~13 min per gate.

  ```bash
  npm install -g @openai/codex
  codex login
  ```

  Also requires the `/review-gate` skill (3-pass orchestrator). Sourced from the same harness that ships gstack — see [gstack docs](https://github.com/garryslist/gstack) for installation. If `/review-gate` is not available in your skills, the gate step skips with a warning. `/review-gate` remains a compatibility alias.

## Installation

### Quick install

```bash
git clone https://github.com/austinmao/feature-fix-swarm.git
cd feature-fix-swarm
bash setup.sh
```

The installer copies skills, scripts, and prompts into your project. It checks prerequisites and warns if anything is missing.
If you approve the prompt, it bootstraps the missing dependencies in one pass.
It also verifies the installed runner, adversary adapter, hard-bound helper,
and hang guard byte-for-byte and registers the guard for both Claude and Codex.
To reconcile only those runtime levers in an existing consumer repo, run
`bash setup.sh --reconcile-consumer /absolute/path/to/repo`.

### Manual install

```bash
# Copy skills into the Claude-side skill directory
cp -r skills/* .claude/skills/

# Copy scripts to your project root
cp -r scripts/* scripts/

# Copy prompts
cp -r prompts/* prompts/

# Add .ralph/ to .gitignore (QA failure artifacts)
echo '.ralph/' >> .gitignore
```

## Usage

### Build a feature with per-phase QA

```bash
# 1. Write a spec (plan.md is required, spec.md is optional)
mkdir -p specs/042-user-auth
# ... write specs/042-user-auth/plan.md

# 2. Decompose into tasks (host-aware model labels are rendered automatically)
/spec-decompose 042

# 3. Review tasks.md, then implement with QA enforcement
/feature-implement 042 --qa-loop

# 4. See the QA plan without executing
/feature-implement 042 --qa-loop --dry-run

# 5. Skip e2e tests (backend-only feature)
/feature-implement 042 --qa-skip e2e
/feature-implement 042 --qa-openclaw
/feature-implement 042 --qa-telegram

# 6. Run only code review and security
/feature-implement 042 --qa-only review,security
```

### Fix a bug post-ship

```bash
# Basic: investigate + fix + verify
/fix "auth redirect broken after login"

# Dry run: investigate + plan, don't apply fix
/fix "500 on /api/users" --dry-run

# Complex bug: use /plan-eng-review for architectural analysis
/fix "race condition in checkout" --plan

# Scope-lock to specific files
/fix "form validation broken" --scope=src/components/Form.tsx,src/lib/validate.ts

# Skip full QA (just verify the affected area)
/fix "typo in error message" --no-qa
```

### End-to-end pipeline

```bash
# One command: autoplan -> decompose -> implement (with QA) -> qa -> ship -> canary
/feature 042
```

## Configuration

| Env var | Default | What it does |
|---------|---------|-------------|
| `RALPH_MAX_RETRIES` | `3` | Max retry attempts per phase on QA failure |
| `RALPH_AUTO_QA` | `1` | Set to `0` to disable PostToolUse auto-qa hook |
| `RALPH_EXECUTOR` | auto | Force `ruflo` coordination or direct `host-cli` execution |
| `RALPH_DEBOUNCE_SECS` | `30` | Quiet window before auto-qa fires |

## Flags reference

| Flag | Works with | Effect |
|------|-----------|--------|
| `--qa-loop` | /feature-implement | Enable per-phase QA (default ON) |
| `--no-qa-loop` | /feature-implement | Disable the QA loop entirely |
| `--qa-skip e2e,security` | /feature-implement | Skip specific QA dimensions |
| `--qa-only review` | /feature-implement | Run only these QA dimensions |
| `--dry-run` | /feature-implement, /fix | Plan without executing |
| `--resume` | /feature-implement | Pick up from last failure |
| `--ruflo` | /feature-implement | Force Ruflo coordination with active host CLI execution |
| `--one` | /feature-implement | Execute only the next task |
| `--plan` | /fix | Use /plan-eng-review for complex bugs |
| `--no-qa` | /fix | Skip full /qa, only run /qa-only |
| `--no-review-gate` | /fix, /feature | Skip cross-model adversarial review (default-on) |
| `--scope=files` | /fix | Scope-lock investigation to specific files |

## The QA dimensions

feature-fix-swarm runs 5 QA dimensions, 2 deterministic + 3 LLM:

| Dimension | Type | Model | What it checks | Pass/fail |
|-----------|------|-------|---------------|-----------|
| unit | deterministic | -- | `vitest run` / `pytest -x` on changed files | tests green |
| integration | deterministic | -- | API contract tests on changed endpoints | contracts hold |
| e2e | LLM agent | sonnet | Browser tests via $B (gstack browse) | journeys complete |
| review | LLM agent | sonnet | Code review for logic errors, CRITICAL/HIGH | no CRITICAL/HIGH |
| security | LLM agent | sonnet | OWASP Top 10 scan on diff | no exploitable vulns |

Unit and integration are free ($0). The 3 LLM agents cost ~$0.15/phase total.

Those dimensions are rendered from the normalized task graph, so the active host can swap in the right model ladder without changing the workflow definition.

## The learning system

Every successful `/fix` stores the bug pattern in two places:

1. **ruflo memory** (vector search) -- semantic similarity matching
2. **gstack learnings** (cross-session) -- structured patterns with confidence scores

The next time someone hits a similar bug, `/fix` Step 0.5 searches both systems before investigating:

```
[FIX] Prior fix patterns found:
  1. [pattern] auth-redirect-middleware (confidence 9/10, 2026-04-17)
     Root cause: session token not refreshed after OAuth callback.
     Fix: add token refresh in auth/callback.ts:42.
```

The investigation starts with this context instead of from scratch. Fix times drop as patterns accumulate.

## Cost

| Scenario | Typical cost |
|----------|-------------|
| Per-phase QA (deterministic hooks) | $0.00 |
| Per-phase QA (3 LLM agents) | ~$0.15 |
| Full feature, 20 tasks, 5 phases | ~$0.75 QA overhead |
| Bug fix, trivial (1 file) | ~$0.10 |
| Bug fix, moderate (2-5 files) | ~$0.50 |
| Bug fix, complex (5+ files + eng review) | ~$2.00 |

## What's in the box

```
feature-fix-swarm/
  skills/           16 Claude Code SKILL.md files
    fix/              investigate + fix + verify loop
    code-uplift/      review + refactor + test-uplift for existing code (findings-driven)
    testing-policy/   canonical testing doctrine — mock ladder, browser gate, coverage floor
    feature-implement/  task executor with per-phase QA gates
    feature/          end-to-end pipeline (autoplan through canary)
    feature-spec/     spec-first pipeline (speckit.specify -> plan -> clarify, TDD/BDD/E2E)
    spec-decompose/   spec to tasks.md decomposition (mandatory review-gate phase gates)
    plan-decompose/   description/plan to tasks.md — no speckit interview
    swarm/            ad-hoc task swarm — no spec dir required; classify + execute inline tasks
    review-gate/      host-neutral cross-model pre-merge review gate (3 passes)
    goal-wrap/        bundle current work into a tracked, anti-drift /goal prompt
  scripts/          22 scripts (bash + harness-audit.py)
    qa-swarm.sh       QA manifest builder (2 hooks + 3 LLM agent prompts)
    ralph-retry.sh    investigate -> fix -> re-qa retry loop
    browser-proof.sh  browser-QA evidence gate
    harness-audit.py  0-100 installed-harness scorer (dangling symlinks, drift)
    gsd/              gsd-run lifecycle + adversary/canary/liveness/security levers
    hooks/            worktree GC, debounced auto-QA, delegation-enforcer auto-pin
  prompts/          4 LLM agent prompts
    qa-e2e.md         browser test agent
    qa-review.md      code review agent
    qa-security.md    OWASP security scan agent
    decompose-spec.md canonical spec decomposition prompt (host-aware model ladder, review-gate mandate)
  docs/             4 reference docs
    commands.md       full command reference
    pipeline.md       pipeline architecture + diff-scope classification
    qa-ralph-loop.md  RALPH investigate-fix-retest loop
    tdd-bdd-guide.md  research-backed TDD/BDD practices
  lib/              shared Python library (dispatch.py: task parsing, model routing, cost estimation)
  examples/         synthetic test spec for dogfooding
```

## License

MIT

## Credits

Built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code) by Anthropic, [gstack](https://github.com/garryslist/gstack) by Garry Tan, and [ruflo](https://github.com/ruvnet/claude-flow) (claude-flow) by rUv.
