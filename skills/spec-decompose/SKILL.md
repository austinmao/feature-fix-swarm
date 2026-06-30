---
name: spec-decompose
description: "Decompose an approved feature spec into specs/NNN/tasks.md using Sonnet + the canonical decomposition prompt"
version: "1.1.0"
allowed-tools:
  - Read
  - Write
  - Glob
  - Bash
  - Agent
---

# spec-decompose — Turn an approved spec into an executable task list

## When to invoke

- After `/speckit.plan` writes `specs/NNN/plan.md` and `/autoplan` has reviewed it
- When the user says "decompose spec NNN", "generate tasks for NNN", "break this into tasks"
- Before implementing a feature — tasks.md prevents mid-work tunnel vision

## What it does

1. Locates `specs/NNN-feature-name/` from CLI argument or current git branch
2. Verifies `spec.md` and `plan.md` exist (hard requirements per `/speckit.tasks` contract)
3. Spawns a Sonnet sub-agent with `prompts/decompose-spec.md` + the spec's design artifacts
4. Sub-agent writes the decomposed task list to `specs/NNN/tasks.md`
5. **Each phase ends with a `/review-gate` review task** — blocks the next phase if HIGH/CRITICAL findings
6. If a prior baseline exists, runs `scripts/harness-eval.sh --compare NNN specs/NNN/tasks.md`
7. Reports summary and flags anything suspicious

## Codex-Gate Phase Gates (MANDATORY)

Every phase in `tasks.md` **MUST** end with a review-gate task before the next phase begins.
This is a hard requirement — the suspicious-output check in Step 7 will fail the decomposition if any phase is missing one.

### Canonical review-gate task format

```markdown
- [ ] [model:sonnet] [agent:code-reviewer] /review-gate — review Phase N diff. HIGH/CRITICAL findings block Phase N+1. Address all CRITICAL, fix or defer HIGH. [qa:review-gate] [P]
  - Depends-on: <last implementation task in this phase>
  - Run: /review-gate
  - Gate: no phase transition until exit code 0 (0 CRITICAL, 0 HIGH unaddressed)
```

### Where to place review-gate tasks

- After every implementation phase (Setup, US1…USn, Integration)
- NOT after read-only or planning phases (Research, Architecture Review)
- NOT after Staging/Production/Rollback phases (those use canary/smoke, not review-gate)
- Always the LAST task in the phase before phase boundary comment

### Example tasks.md phase structure

```markdown
## Phase 1 — Setup

- [ ] [model:haiku] Scaffold directory structure and install dependencies
- [ ] [model:sonnet] Write failing unit tests for AuthService (RED — TDD step 1)
- [ ] [model:sonnet] /review-gate — review Phase 1 diff. HIGH/CRITICAL block Phase 2. [qa:review-gate] [P]
  - Depends-on: Write failing unit tests for AuthService

## Phase 2 — US1: User login

- [ ] [model:sonnet] Implement AuthService.login() to pass unit tests (GREEN — TDD step 2)
- [ ] [model:sonnet] Refactor login flow for clarity (REFACTOR — TDD step 3)
- [ ] [model:sonnet] /review-gate — review Phase 2 diff. HIGH/CRITICAL block Phase 3. [qa:review-gate] [P]
  - Depends-on: Refactor login flow for clarity

## Phase 3 — Integration

- [ ] [model:sonnet] Wire AuthService to API route handler
- [ ] [model:sonnet] Add integration test: POST /api/auth/login → 200 + session cookie
- [ ] [model:sonnet] /review-gate — review Phase 3 diff. HIGH/CRITICAL block QA phase. [qa:review-gate] [P]
  - Depends-on: Add integration test
```

## Step-by-step workflow

### Step 1: Resolve target spec

Accept `$ARGUMENTS` as either a spec number (`057`) or a full dir name (`057-onboarding-wizard`). If empty, detect from current git branch.

```bash
SPEC_ARG="${ARGUMENTS:-}"
if [ -z "$SPEC_ARG" ]; then
  BRANCH=$(git branch --show-current 2>/dev/null)
  SPEC_ARG=$(echo "$BRANCH" | grep -oE '^[0-9]{3}' | head -1)
fi
[ -z "$SPEC_ARG" ] && { echo "ERROR: no spec ID. Usage: /spec-decompose NNN"; exit 1; }

SPEC_DIR=$(find specs -maxdepth 1 -type d -name "${SPEC_ARG}-*" 2>/dev/null | head -1)
[ -z "$SPEC_DIR" ] && { echo "ERROR: specs/${SPEC_ARG}-* not found"; exit 1; }

echo "Target: $SPEC_DIR"
```

### Step 2: Verify inputs

```bash
# plan.md is required
[ -f "$SPEC_DIR/plan.md" ] || { echo "ERROR: $SPEC_DIR/plan.md missing. Write one manually or run /speckit.plan"; exit 1; }

# BUG-2 fix (2026-04-16): prompts/decompose-spec.md may be missing in worktrees
# that are behind main. Fall back to git HEAD before erroring.
DECOMPOSE_PROMPT=""
if [ -f "prompts/decompose-spec.md" ]; then
  DECOMPOSE_PROMPT="prompts/decompose-spec.md"
else
  mkdir -p /tmp/harness
  if git show HEAD:prompts/decompose-spec.md > /tmp/harness/decompose-spec.md 2>/dev/null; then
    DECOMPOSE_PROMPT="/tmp/harness/decompose-spec.md"
    echo "WARN: prompts/decompose-spec.md not in worktree; using HEAD copy at $DECOMPOSE_PROMPT"
  else
    echo "ERROR: prompts/decompose-spec.md missing from worktree AND git HEAD"
    exit 1
  fi
fi

# spec.md is optional — warn if missing
if [ ! -f "$SPEC_DIR/spec.md" ]; then
  echo "WARN: $SPEC_DIR/spec.md missing. Sub-agent will extract stories from plan.md or fall back to single user story."
fi
```

Pass `$DECOMPOSE_PROMPT` (not the hardcoded path) to the sub-agent in Step 4.

Only plan.md is hard-required. spec.md is optional (sub-agent falls back per `prompts/decompose-spec.md` "Handling missing spec.md" section).

### Step 3: Handle existing tasks.md

If `$SPEC_DIR/tasks.md` already exists, ask via AskUserQuestion:
- **A)** Overwrite — regenerate from scratch
- **B)** Back up existing then regenerate (`mv tasks.md tasks.md.bak-$(date +%s)`)
- **C)** Write to `/tmp/candidate-NNN.md` for side-by-side comparison

Default recommendation: **B**. Existing work is preserved; new output can be diffed.

### Step 4: Spawn the Sonnet sub-agent

Use the Agent tool with `model: "sonnet"` and `subagent_type: "general-purpose"`. Prompt template (substitute the bracketed values):

```
You are decomposing an approved feature spec into an executable task list. You have no prior context for this conversation.

## Step 1: Read your instructions (authoritative)
`{REPO_ROOT}/prompts/decompose-spec.md`

## Step 2: Read the feature inputs
- `{SPEC_DIR}/spec.md`
- `{SPEC_DIR}/plan.md`
- `{SPEC_DIR}/data-model.md` (if exists)
- `{SPEC_DIR}/research.md` (if exists)
- `{SPEC_DIR}/contracts/` (if exists)

## Step 3: Read format references
- Use any existing `specs/*/tasks.md` as format reference (annotations, QA tiers)
- See `examples/000-qa-ralph-synthetic/tasks.md` in the ralph package for a minimal example

DO NOT read `{SPEC_DIR}/tasks.md` if it exists. Produce your decomposition from spec.md + plan.md only.

## Step 4: Project conventions
`{REPO_ROOT}/CLAUDE.md`

## Step 5: Produce output
Follow the decompose-spec.md prompt at full depth. Write to: `{OUTPUT_PATH}`

## Step 6: MANDATORY — review-gate phase gates
Every implementation phase MUST end with a /review-gate task as the final item before
the next phase begins. Format:

  - [ ] [model:sonnet] [agent:code-reviewer] /review-gate — review Phase N diff. HIGH/CRITICAL block Phase N+1. [qa:review-gate] [P]
    - Depends-on: <last implementation task in this phase>
    - Run: /review-gate
    - Gate: no phase transition until exit code 0 (0 CRITICAL, 0 HIGH unaddressed)

Place review-gate after:
- Setup phase
- Each user story phase (US1, US2, …)
- Integration phase

Do NOT place review-gate after:
- Research/architecture-review phases (read-only, no diff)
- Staging/Production/Rollback phases (use canary/smoke tests there instead)

If you omit a review-gate task from any implementation phase, the decomposition is invalid.

## Step 7: Report
After writing: total tasks, phases, review-gate tasks per phase, [P] count, [US] count,
model distribution, agent distribution, Depends-on lines, file path coverage %.

## Step 8: /design-html task injection (MANDATORY for UI tasks)
Scan every task in the output tasks.md. For any task that builds, designs, or creates
a page, landing page, web UI, marketing page, or HTML view (phrases: "create page",
"design page", "build landing", "implement UI", "design flow", "build page"), insert
a `/design-html` task IMMEDIATELY AFTER it in the same phase:

  - [ ] [model:sonnet] [agent:design/frontend] /design-html — generate production HTML for <page name>. Reads /plan-design-review context from autoplan. [qa:design-review] [P]
    - Depends-on: <the page design task above>
    - Run: /design-html
    - Gate: HTML must pass /design-review in QA phase

Do NOT add /design-html tasks after: API/backend/migration tasks, test tasks, or /review-gate tasks.
```

### Step 5: Run comparison (if backup exists)

```bash
BAK=$(ls -t "$SPEC_DIR"/tasks.md.bak-* 2>/dev/null | head -1)
if [ -n "$BAK" ]; then
  bash scripts/harness-eval.sh --compare "$SPEC_ARG" "$SPEC_DIR/tasks.md" 2>&1 | tail -20
fi
```

### Step 6: Report

Run `bash scripts/harness-eval.sh $SPEC_ARG` to extract current metrics. Print a summary:

```
╔═══════════════════════════════════════════════════════════════╗
║ Spec: NNN-feature-name                                        ║
║ Output: specs/NNN-feature-name/tasks.md                       ║
╠═══════════════════════════════════════════════════════════════╣
║ Total tasks:       XX                                         ║
║ Phases:            XX (Setup → Foundational → N user stories  ║
║                    → Integration → Staging → Prod → Rollback) ║
║ Codex-gate tasks:  XX (one per implementation phase ✓/✗)      ║
║ Parallel markers:  XX                                         ║
║ User stories:      XX (US1..USn)                              ║
║ Model distribution:                                           ║
║   haiku/low:       XX (boilerplate)                           ║
║   sonnet/med:      XX (default)                               ║
║   sonnet/high:     XX (complex)                               ║
║   opus/max:        XX (architecture)                          ║
║ Depends-on lines:  XX                                         ║
║ File path coverage: XX%                                       ║
╠═══════════════════════════════════════════════════════════════╣
║ Staging phase:     [yes/no]                                   ║
║ Production phase:  [yes/no]                                   ║
║ Rollback phase:    [yes/no]                                   ║
╚═══════════════════════════════════════════════════════════════╝
```

### Step 7: Flag suspicious output

Warn if any of:
- **FAIL: missing review-gate** — any implementation phase has no `/review-gate` task as its final item. Offer to patch missing gates automatically.
- **WARN: missing /design-html** — any task that designs/creates a page lacks an immediately following `/design-html` task. Offer to auto-insert per the Step 8 format.
- No `[model:]` annotations — sub-agent didn't follow prompt; offer to regenerate
- No `Depends-on:` lines — TDD ordering likely broken
- `[model:opus]` outside architecture/debugging phases — probable over-escalation
- Missing staging/prod/rollback phases — template not followed
- Fewer than 5 tasks — probably under-decomposed (threshold dropped from 10 based on 2026-04-16 smoke test — 7-task specs are legitimate)
- More than 60 tasks — feature should be split into multiple specs
- If `plan.md` Non-goals explicitly states "no tests" — suppress the "under-decomposed" warning
- No `[qa:]` annotations — sub-agent didn't assign QA dimensions; defaults will apply (e2e, review, security)
- review-gate tasks in Staging/Production/Rollback phases — those phases use canary/smoke, remove them

### Step 8: Next step

Output:
```
Next: review `$SPEC_DIR/tasks.md`, then invoke `/feature-implement` or start executing tasks manually.

Each implementation phase ends with /review-gate. HIGH/CRITICAL findings block the next phase.
Run: /review-gate
Docs: https://github.com/austinmao/feature-fix-swarm/blob/main/docs/commands.md#review-gate
```

## Edge cases

- **Multi-directory collision:** two dirs match (`057-onboarding-wizard` and `057-other`). Error; require explicit name.
- **spec.md empty:** refuse. User must complete `/speckit.specify` first.
- **plan.md lacks tech stack:** warn but proceed.
- **Sub-agent timeout (>10 min):** abort, preserve partial output at `/tmp/spec-decompose-aborted.md`.
- **prompts/decompose-spec.md missing:** fatal. Recover via `git checkout prompts/decompose-spec.md`.
- **Sub-agent omits review-gate:** Step 7 catches this. Offer to auto-insert gates with correct `Depends-on:` pointing to the last task in each phase.

## Non-goals

- Does NOT implement tasks (use `/feature-implement`)
- Does NOT generate spec.md or plan.md (use `/speckit.specify` / `/speckit.plan`)
- Does NOT sync to Linear (handled by `post-spec-write.sh` after Write)
- Does NOT review architecture (use `/autoplan`)
- Does NOT run `/review-gate` itself — it only ensures the gate tasks exist in tasks.md so `/feature-implement` runs them at the right phase boundary

## Why a standalone skill, not a meta-skill

Decomposition is self-contained and cacheable. A `/feature-start` meta-skill would be more convenient but locks in the full pipeline. Keeping decomposition standalone lets users regenerate tasks.md without redoing spec/plan, and lets Sonnet be invoked with fresh context per decomposition.
