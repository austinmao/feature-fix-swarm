---
name: swarm
description: "Ad-hoc parallel task executor. Pass natural language tasks directly (or --tasks-file); independent tasks use concurrent host-native volume/execution/judgment roles and dependent tasks run serially. No spec directory required; use feature-implement for full-feature work."
version: "2.1.0"
permissions:
  filesystem: write
  network: false
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Agent
metadata:
  openclaw:
    requires:
      bins: []
      env: []
---

# /swarm — Ad-hoc parallel task executor

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-5.6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

At entry, make one opportunistic, fail-soft `bash scripts/gsd/reconcile.sh` pass; never block on its result.

One command. Pass task descriptions, get model classification, parallel execution,
and a structured result. No spec pipeline, no `.planning/` prerequisite.

## When to invoke

- "swarm these tasks", "run these in parallel", "do all of these"
- Multi-task batch execution without the full spec pipeline
- Quick automation: lint + test + docs + cleanup at once

For anything that IS a feature (multi-phase, TDD, ship tail): use
`/feature-implement` — the gsd loop owns that shape.

## Invocation

```
/swarm "task1" "task2" "task3"     # classify + execute
/swarm --tasks-file PATH           # md checklist or one-per-line
/swarm ... --dry-run               # print annotated plan, don't execute
/swarm ... --sequential            # serial execution
/swarm ... --model-request '{"kind":"tier","name":"judgment"}'
```

## Workflow

### Step 1: Classify

Per task, assign a typed workload request: volume for mechanical/search,
execution for default development, judgment for architecture/security/review.
Resolve through the active host and mark `[P]` when tasks share no files and
no ordering dependency.

### Step 2: Execute

- `[P]` groups → concurrent host-native subagent calls in one wave, using a
  small-toolset named role where one fits.
- Dependent tasks → serial, each receiving the prior result.
- Delegation prompt = Goal · Scope (in/out of bounds) · return contract
  (scout ≤15 / build ≤20 / deep ≤40 lines) · done-means.

### Step 3: Verify + report

Each task's done-means check runs before it reports done (test/lint output:
failures only). Report table: task → model → outcome → evidence line.

## Removed in v2.0.0

Ruflo swarm_init/agent_spawn coordination, hooks_model-route dynamic routing
(static ladder + explicit override instead), memory_/agentdb_ recall-store calls,
RUFLO_REQUIRED plumbing.
