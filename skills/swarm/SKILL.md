---
name: swarm
description: "Ad-hoc parallel task executor. Pass natural language tasks directly (or --tasks-file); independent tasks use concurrent host-native volume/execution/judgment roles and dependent tasks run serially. No spec directory required; use feature-implement for full-feature work."
version: "2.2.10"
permissions:
  filesystem: write
  network: true
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

- Codex: `$skill`, Codex collaboration roles, and GPT-6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

## Init gate

Before any other step, run the advisory init guard and relay its output
verbatim:

```bash
bash "$(git rev-parse --show-toplevel)/scripts/gsd/init-guard.sh" || true
```

If it printed `INIT-GUARD:` warnings, offer `/ffs-init` before proceeding in
interactive sessions (declining proceeds anyway); headless, spawned, and
autonomous runs relay the warnings once and continue. Advisory only — never
a block, never an exit-code change.

<!-- ffs:cloud:begin -->
## Cloud offload (opt-in)

This block applies ONLY when `--cloud` (policy `auto` by default, or
`--cloud=prefer|required`) or the `FFS_CLOUD` environment variable is present.
Otherwise skip it entirely and continue below exactly as before.

1. **Normalize tasks.** Convert every task to the normalized JSON of
   `docs/cloud-delegation-contracts.md` §2.1 (`id`, `title`, `prompt`, `tier`,
   `model_request`, `owns`, `owns_source`, `est_minutes`, `needs_secrets`,
   `local_only`, `depends_on`, `kind`, `verify_cmd`). Read these annotations
   from the task text: `[owns: a/**, b.py]` `[est: 45m]` `[needs-secret: NAME]`
   `[local]` `[after: t0]` `[verify: cmd]` `[id: x]`. Estimate a missing `owns`
   or `est` and set `owns_source: "estimated"`. Never invent `verify`: an absent
   one stays absent (the task then runs locally). Write the list to a temp
   file (`FILE`); `P` is the policy.
2. **Plan.** Set `ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"`:
   the main worktree, which every dispatcher verb requires (from a linked worktree
   `git rev-parse --show-toplevel` is the wrong directory and is refused as
   `NOT_MAIN_WORKTREE`). Run `bash scripts/gsd/cloud-dispatch.sh plan --tasks FILE --policy P --root "$ROOT" --with-doctor`.
   If it exits 3 with `CLOUD-REFUSED: SECRET_IN_PROMPT` (a task id looks like a
   secret), that is terminal for this invocation: relay the redacted refusal
   line and STOP. Dispatch nothing, cloud or local, under `auto`, `prefer` and
   `required` alike, and under `--dry-run`. Step 6's local fallback does not
   apply: a retry of an already started run with an edited batch is refused here
   before `start` can say `RUN_EXISTS`, and its unchanged tasks may be running in
   the cloud.
3. **`--dry-run`.** Print the plan table (groups, route, provider, reasons,
   refusals, preconditions) and STOP. Do not run the reconcile pass below, do
   not start anything, do not execute any task.
4. **Without `--dry-run`.** Show the operator the groups, routes, providers,
   exposed secret names and budgets from the plan, then record grants:
   - Interactive session: ask ONE question covering the task list, the providers
     and every exposed secret name. On a yes, record exactly those keys, from the
     repository you pass as `--root` (`cd "$ROOT"`
     first: the dispatcher reads that repository's grant store, not the shell's directory):
     `python3 lib/gates.py grant <run> --action cloud-dispatch:<provider> --action push:origin/ffs/cloud/<run> [--action secret-use:<NAME> ...] --ttl-hours 24 --reason "interactive /swarm --cloud"`
   - Autonomous or headless run: never self-grant. Rely on grants that already
     exist; `start` refuses when one is missing.
5. **Start cloud groups.** `bash scripts/gsd/cloud-dispatch.sh start --run <run> --tasks FILE --policy P --root "$ROOT"`.
   On exit 0 it prints `CLOUD:started run=<run> tasks=N` and then
   `CLOUD:local tasks=<ids>`: the comma-separated ids of every task it routed
   locally, empty when there are none. `start` routes again, and under `auto`
   host pressure can move a task between the plan and `start`, so the plan's
   local groups may be stale. Run locally exactly the ids in `CLOUD:local tasks=`
   (grouped as Step 2 does today), never from the plan, and never a task that
   line does not name. An empty value means run no local task. If `start`
   exited 0 but that line is missing or cannot be parsed, run no local task,
   report the run id, and tell the operator the local list is unknown.
   If `start` exits 78, it printed `CLOUD-ERROR: <CODE> ...` on **stderr**
   (for example `INTEGRATION_REF_UNCONFIRMED`: an identical retry found the
   integration ref a crashed start left behind). Relay that stderr, run no task
   (cloud or local) and stop for the operator to inspect what the message names.
   Do not call `start` again until the operator has done so and says to proceed.
   Step 6 does not apply: exit 78 is an error, not a refusal.
6. **Refusals.** Exit code 3 means refused. Relay the `CLOUD-REFUSED:` line.
   Under `auto` or `prefer`, continue with every task locally; under
   `required`, stop. **A refusal from `plan` falls back to local only when the
   run does not exist:** first run `bash scripts/gsd/cloud-dispatch.sh status --run <run> --root "$ROOT"` (the run id of steps 4 and 5);
   only its stderr line `CLOUD-ERROR: LEDGER_MISSING` proves there is no run.
   Any other result (a status document, or any other error) means a run with
   this id may already be running tasks in the cloud: run nothing, cloud or
   local, relay the refusal and the status, and ask the operator. One refusal
   needs no `status`: `CLOUD-REFUSED: NOT_A_REPO` (the directory is not a git
   repository, so no ledger can exist) falls back to local under `auto` and
   `prefer`, and stops under `required`.
   **Exception: `CLOUD-REFUSED: SECRET_IN_PROMPT`** (from
   `plan` or `start`) is terminal as in step 2: dispatch nothing, cloud or local.
   **Exception: `CLOUD-REFUSED: RUN_EXISTS`** is not a
   refusal to offload. The run was already started (a retry after a lost
   acknowledgement, or a reused run id) and its tasks may be running in the
   cloud. Never run those tasks locally and never call `start` again for that
   run. The refusal line ends with `batch=same`, `batch=same local-claimed=<ids>`,
   `batch=other` or `batch=unknown`, optionally followed by
   `orchestrator=restarted` or `orchestrator=restart-failed cause=<ERRNO>`: `start` itself compared this batch (every task id,
   cloud and local, its content, its dependencies and its order) with the whole
   batch the first attempt started. `batch=same` means an exact match; a
   superset, a subset or an edited batch is `batch=other`, and a run started by
   an older release (no stored batch) or whose inputs are incomplete is
   `batch=unknown`. On `batch=same`, `orchestrator=restarted` means `start`
   found no orchestrator for the run and started one. `orchestrator=restart-failed`
   means it found none and could not start one (a resource or filesystem
   failure; `cause=` is the errno name, or `OSERROR` when there is none): the run is active with no orchestrator
   and no lifecycle record, so `reconcile.sh` has nothing to restart, and the
   local claims stand. This branch ends after you report it: tell the operator
   the run id and the cause to fix, run no `reconcile.sh`, no local task and no
   cloud task, and do nothing else. The same `start` (same run id, same tasks
   file) may be repeated, once, after the cause is fixed.
   Otherwise run `bash scripts/gsd/cloud-dispatch.sh status --run <run> --json` and treat
   the run as started only on `batch=same`: report it as in step 8, and if
   `orchestrator.alive` is false and the line carried no `orchestrator=` token,
   run `bash scripts/gsd/reconcile.sh` once so a dead
   orchestrator is restarted. `local-claimed=<ids>` names the local tasks the
   first attempt claimed and may already have started: do not start a claimed
   local task again without the operator's confirmation, and run none of the
   local groups that contain one until the operator has said which to run.
   Never run a task the ledger holds. On `batch=other` or
   `batch=unknown`, stop and tell the operator the run id is taken: never
   start these cloud tasks again under a new run id until `status` shows the
   run's tasks terminal, and ask the operator how to proceed. In every case,
   run no cloud task locally.
7. **Hard limits.** Never call `claude --cloud` or `codex cloud` directly. Never
   run `cloud-dispatch.sh adopt`, `abandon` or `resolve`: they are
   operator-only. When a task needs one, print the exact suggested command for
   the operator and leave it.
8. **Step 3 additions.** Report each cloud task from
   `bash scripts/gsd/cloud-dispatch.sh status --run <run>` (state, provider,
   URL, blocker). Print the integration ref `ffs/cloud/<run>/integration` and
   a suggested merge command for the operator. Merge nothing, push nothing
   else, deploy nothing.
<!-- ffs:cloud:end -->

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
/swarm ... --cloud[=auto|prefer|required]   # opt-in cloud offload (see cloud block)
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
