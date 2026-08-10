# Run healing

This guide describes bounded recovery for a durable GSD run. FFS never installs a scheduler: without the operator cron below, a waiting run wakes when an FFS entrypoint is next invoked.

## Lifecycle

| State | Legal next states |
| --- | --- |
| `running` | `waiting`, `done`, `failed`, `quarantined` |
| `waiting` | `runnable`, `failed`, `quarantined` |
| `runnable` | `running`, `failed`, `quarantined` |
| `done`, `failed`, `quarantined` | none |

Wake conditions are `time`, `wall-decided`, `ci-completed`, and `manual`. A reconcile launch consumes `budgets.respawns`; session wake consumes `budgets.wakes`; CI retry consumes `budgets.ci_reruns`.

## Controls and signals

| Control | Default | Meaning |
| --- | --- | --- |
| `FFS_RECONCILE` | `on` | Set to `off` for a typed no-op reconcile pass. |
| `FFS_LIFECYCLE` | `on` | Prevents new checkpoints; existing recovery records remain readable. |
| `FFS_RESPAWN_MAX` | `1` | Number of in-process runner retries. |
| `FFS_RESPAWN_MIN_SECS` | `600` | Retry timeout floor, capped by the original timeout. |
| `FFS_CI_RERUN_MAX` | `2` | Durable CI rerun budget. |
| `FFS_CI_GH_ERR_MAX` | `5` | Consecutive GitHub API errors allowed before the lifecycle record fails. |
| `FFS_RECONCILE_STALE_SECS` | `300` | Dead launcher age before stale-running recovery is eligible. |
| `PLAN_WALL_AWAIT_POLL` | `15` | Maximum seconds between bounded wall-verdict probes. |

Look for `LIFECYCLE:`, `SESSION-WAKE:`, `CI-WATCH:`, `WALL-AWAIT:`, `RECONCILE:`, and `GSD-RUN:RESPAWN` lines. Normal no-ops use rc 0; lifecycle validation failures use rc 1; an unresolved wall-await returns rc 75 and a decided blocked wall returns rc 20. A reconciler claim already held by another pass is deliberately a typed rc 0 no-op.

An operator may schedule a pass, for example:

```cron
*/5 * * * * cd /path/to/feature-fix-swarm && bash scripts/gsd/reconcile.sh
```

FFS never installs that cron entry. The time, CI, and wall mechanisms can be deleted when the respective vendor supplies durable, authenticated delayed-resume, CI completion webhooks, and plan-review callbacks.

## Evidence and boundary

Current evidence is n=1, so it is directional rather than a reliability guarantee. The spec-011 retro measures intervention-free completion rate and the wall-clock/active ratio.

Lifecycle and verdict records share the filesystem privilege of the gate code. Shape validation and the argv allowlist reduce cheap forgery, but a same-privilege writer is explicitly outside this threat model.
