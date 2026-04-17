---
name: spec-decompose
description: "Decompose an approved feature spec into specs/NNN/tasks.md using Sonnet + the canonical decomposition prompt"
version: "1.0.0"
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
5. If a prior baseline exists, runs `scripts/harness-eval.sh --compare NNN specs/NNN/tasks.md`
6. Reports summary and flags anything suspicious

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

## Step 6: Report
After writing: total tasks, phases, [P] count, [US] count, model distribution, agent distribution, Depends-on lines, file path coverage %.
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
- No `[model:]` annotations — sub-agent didn't follow prompt; offer to regenerate
- No `Depends-on:` lines — TDD ordering likely broken
- `[model:opus]` outside architecture/debugging phases — probable over-escalation
- Missing staging/prod/rollback phases — template not followed
- Fewer than 5 tasks — probably under-decomposed (threshold dropped from 10 based on 2026-04-16 smoke test — 7-task specs are legitimate)
- More than 60 tasks — feature should be split into multiple specs
- If `plan.md` Non-goals explicitly states "no tests" — suppress the "under-decomposed" warning
- No `[qa:]` annotations — sub-agent didn't assign QA dimensions; defaults will apply (e2e, review, security)

### Step 8: Next step

Output: "Next: review `$SPEC_DIR/tasks.md`, then invoke `/feature-implement` or start executing tasks manually."

## Edge cases

- **Multi-directory collision:** two dirs match (`057-onboarding-wizard` and `057-other`). Error; require explicit name.
- **spec.md empty:** refuse. User must complete `/speckit.specify` first.
- **plan.md lacks tech stack:** warn but proceed.
- **Sub-agent timeout (>10 min):** abort, preserve partial output at `/tmp/spec-decompose-aborted.md`.
- **prompts/decompose-spec.md missing:** fatal. Recover via `git checkout prompts/decompose-spec.md`.

## Non-goals

- Does NOT implement tasks (use `/feature-implement`)
- Does NOT generate spec.md or plan.md (use `/speckit.specify` / `/speckit.plan`)
- Does NOT sync to Linear (handled by `post-spec-write.sh` after Write)
- Does NOT review architecture (use `/autoplan`)

## Why a standalone skill, not a meta-skill

Decomposition is self-contained and cacheable. A `/feature-start` meta-skill would be more convenient but locks in the full pipeline. Keeping decomposition standalone lets users regenerate tasks.md without redoing spec/plan, and lets Sonnet be invoked with fresh context per decomposition.
