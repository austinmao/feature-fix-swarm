---
name: swarm
description: "Ad-hoc parallel task executor. Pass natural language tasks directly (or --tasks-file); independent tasks run as concurrent native Task() agents (haiku=mechanical, sonnet=default, opus=adversarial/architecture), dependent tasks run serially. No spec directory required. Ruflo coordination removed in v2.0.0 (spec 002) — for full-feature work use /feature-implement (gsd loop) instead."
version: "2.0.0"
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
/swarm ... --model-override opus   # override all model assignments
```

## Workflow

### Step 1: Classify

Per task, assign model by the static ladder (haiku = mechanical/search,
sonnet = default dev, opus = architecture/security/adversarial) and mark `[P]`
when tasks share no files and no ordering dependency.

### Step 2: Execute

- `[P]` groups → concurrent native `Task()`/Agent calls in ONE message, small-toolset
  named agent types where one fits (never general-purpose on constrained machines).
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
