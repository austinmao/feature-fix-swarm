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
| `FFS_SESSION_WAKE` | `on` | Set to `off` to skip banner checkpointing in the runner's failed-drive path. |
| `FFS_SESSION_WAKE_MAX_SECS` | `21600` | Cap on how far ahead a parsed reset time may schedule the wake. |
| `FFS_SESSION_WAKE_MAX_ATTEMPTS` | `4` | Durable wake budget (`budgets.wakes`) seeded on first checkpoint. |
| `PLAN_WALL_AWAIT_POLL` | `15` | Maximum seconds between bounded wall-verdict probes. |
| `PLAN_WALL_AWAIT_MAX` | `6` | Pending `--await` returns allowed per phase before rc 76 `WALL-AWAIT:attempts-exhausted`; the counter resets on any decided outcome, so a decided wall always reports through. |

Look for `LIFECYCLE:`, `SESSION-WAKE:`, `CI-WATCH:`, `WALL-AWAIT:`, `RECONCILE:`, and `GSD-RUN:RESPAWN` lines. Normal no-ops use rc 0; lifecycle validation failures use rc 1; an unresolved wall-await returns rc 75 (rc 76 once `PLAN_WALL_AWAIT_MAX` pending returns are spent — checkpoint instead of polling further) and a decided blocked wall returns rc 20. A terminal record (`done`/`failed`/`quarantined`) reports `RECONCILE:terminal run=<id> state=<state>`, never `still-waiting`. A reconciler claim already held by another pass is deliberately a typed rc 0 no-op.

An operator may schedule a pass, for example:

```cron
*/5 * * * * cd /path/to/feature-fix-swarm && bash scripts/gsd/reconcile.sh
```

FFS never installs that cron entry. Per-mechanism sunset: the time-wake mechanism can be deleted when the vendor supplies durable, authenticated delayed-resume; the CI mechanism when CI completion webhooks reach the runner; the wall mechanism when plan-review callbacks exist; the respawn loop when the vendor runner reports and retries its own dead executors; the plan-length gate when plan review itself enforces a length budget upstream.

## Evidence and boundary

Current evidence is n=1, so it is directional rather than a reliability guarantee. The spec-011 retro measures intervention-free completion rate and the wall-clock/active ratio.

Lifecycle and verdict records share the filesystem privilege of the gate code. Shape validation and the argv allowlist reduce cheap forgery, but a same-privilege writer is explicitly outside this threat model.
