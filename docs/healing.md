# Run healing

This guide describes bounded recovery for a durable GSD run. A reconcile pass is one-shot; without a scheduler a waiting or dead run wakes only when an FFS entrypoint is next invoked. FFS installs a scheduler ONLY on explicit opt-in: `scripts/gsd/reconcile-agent.sh install` (see below), offered by `/ffs-init` and required (or replaced by a session cron) by `/feature-implement --autonomous`.

## Never-stop continuation (2026-10-25)

Three failure classes are counted apart, so raising one budget never lets another class run away:

| Class | Signal | Budget |
| --- | --- | --- |
| Step failure | nonzero rc other than the rows below, or rc 124 timeout | `FFS_STEP_ATTEMPTS=5` per step (`drive.sh`); `FFS_RESPAWN_MAX=2` inner retries per `gsd-run` |
| Environment death | rc 129/137/143 — signal, OOM, Claude Code background-task memory reap, host reboot — of the drive, the runner or the supervisor | `FFS_ENV_DEATH_MAX=8` per step, backoff `FFS_ENV_BACKOFF_BASE_SECS=60` × 2^k capped `FFS_ENV_BACKOFF_CAP_SECS=900`; never consumes a step attempt |
| User-input block | rc 78 (tuple drift, missing grant/secret, refused launch) | 0 retries; typed `pending`, drive moves on |

Runaway guards (all per step): `FFS_NO_PROGRESS_MAX=3` consecutive failures with no new commit and an identical failure fingerprint quarantine the step; `FFS_STEP_LAUNCH_CEILING=12` caps launches across every class; `FFS_WALL_WAIT_MAX=24` × `FFS_WALL_DEFER_SECS=900` bounds rc 75/76 plan-wall waits. `PLAN_WALL_MAX_ROUNDS=2` is unchanged — it bounds review rounds, not environment deaths.

Note on rc 137: `run-bounded.sh` folds a child's 137 into 124 (its own `--kill-after` escalation is indistinguishable from an external SIGKILL), so inside `gsd-run.sh` a SIGKILLed drive takes the rc-124 respawn path; a SIGKILLed *runner* is seen as 137 by `drive.sh` and charged as an environment death.

Mechanisms:

- `gsd-run.sh` writes a `running` lifecycle record at launch when `GSD_RUN_ID` is explicit (`FFS_LIFECYCLE_LAUNCH=off` suppresses it under a supervisor). A death that skips every trap leaves that record `running` with a dead pid, which `reconcile.sh` relaunches as a resume (`GSD_RESUME=1`, charging `env_deaths`). A child drive killed by signal while the runner survives is relaunched in-process (`GSD-RUN:ENV-DEATH n=k/M rc=`) after backoff and a `host-pressure.sh wait`.
- `scripts/gsd/drive.sh` is the detached supervisor for multi-step drives (`start --verb plan-phase|execute-phase --phases A-B --detach`). It runs in its own session/process group (python `os.setsid`; macOS ships no `setsid`), reuses one gsd run id per step so retries resume, records its own lifecycle record so reconcile relaunches it (`drive.sh resume --run <id>`), and on a blocked or quarantined step appends `drive-<id>.pending.md` then continues (`--on-blocked next`) or parks as `waiting(manual)` (`--on-blocked park`). `drive.sh status` prints `DRIVE:alive|dead|done|parked` with counters.
- `--project` and explicit host/model/network/timeout settings are restored from drive state on every resume, including launchd recovery. Only the named invocation settings are stored, never the full environment. `--after-step` runs in the exact runner worktree when it exists, so phase gates inspect the produced plans; otherwise it runs in the invoking checkout.
- `scripts/gsd/host-pressure.sh` is memory-aware admission: `check` returns rc 75 when swap ≥ `FFS_PRESSURE_SWAP_PCT=85` AND free < `FFS_PRESSURE_FREE_PCT=10`, or free < `FFS_PRESSURE_FREE_CRIT_PCT=5`. `reconcile.sh` then `defer`s the record (budget-neutral `waiting(time)`, `FFS_PRESSURE_DEFER_SECS=300`) instead of relaunching into the reap; `drive.sh` and the runner `wait` (up to `FFS_PRESSURE_MAX_WAIT_SECS=7200`, then launch anyway, loudly). It observes only and never kills anything.
- `scripts/gsd/reconcile.sh` detaches every relaunch into its own session, so a reconcile pass run from inside a Claude Code Bash call cannot take the relaunched run down with it.
- `scripts/gsd/reconcile-agent.sh install|uninstall|status [--repo PATH] [--interval SECS]` installs the per-user launchd agent `org.featurefixswarm.reconcile.<sha8(repo)>` (`StartInterval` 300, PATH captured at install, log `.planning/run-state/reconcile-agent.log`). Linux gets the cron line printed. The agent runs `/bin/bash scripts/gsd/reconcile.sh`; see [Known issue](#known-issue-reconcile-relaunch-fails-on-bash-32).

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
| `FFS_RESPAWN_MAX` | `2` | Number of in-process runner retries for step failures (rc 124 or zero-commit nonzero). `drive.sh` runs each step with `FFS_DRIVE_INNER_RESPAWN_MAX` (default `0`) instead and owns the attempt budget itself. |
| `FFS_DRIVE_INNER_RESPAWN_MAX` | `0` | `drive.sh`: value it passes to the runner as `FFS_RESPAWN_MAX` for each step launch (`drive.sh:231`). |
| `FFS_ENV_DEATH_MAX` | `8` | Per-step budget for signal deaths (rc 129/137/143); also the default `env_deaths` seeded on every lifecycle record. |
| `FFS_ENV_BACKOFF_BASE_SECS` / `FFS_ENV_BACKOFF_CAP_SECS` | `60` / `900` | Exponential backoff between env-death relaunches. |
| `FFS_LIFECYCLE_LAUNCH` | `on` | `off` suppresses the runner's launch-time lifecycle record (a supervisor owns the record). |
| `FFS_STEP_ATTEMPTS` | `5` | `drive.sh` step-failure attempts per step. |
| `FFS_NO_PROGRESS_MAX` | `3` | `drive.sh`: identical no-commit failures before quarantine. |
| `FFS_STEP_LAUNCH_CEILING` | `12` | `drive.sh`: hard cap on launches of one step across all classes. |
| `FFS_WALL_WAIT_MAX` / `FFS_WALL_DEFER_SECS` | `24` / `900` | `drive.sh`: bounded rc 75/76 plan-wall waits. |
| `FFS_STEP_BACKOFF_SECS` | `30` | `drive.sh`: pause between step-failure attempts. |
| `FFS_DISK_MIN_MB` | `700` | `drive.sh`: wait (not fail) while free disk, in MB, on the repo's filesystem is below this. An unreadable reading fails open. |
| `FFS_DISK_WAIT_SECS` | `300` | `drive.sh`: seconds to sleep between free-disk re-checks (`drive.sh:66`). |
| `FFS_DISK_MAX_WAIT_SECS` | `7200` | `drive.sh`: cap in seconds on the total disk wait. When spent, the drive prints `DRIVE:disk-wait-exhausted` and launches anyway (`drive.sh:67`). |
| `FFS_PRESSURE_SWAP_PCT` / `FFS_PRESSURE_FREE_PCT` / `FFS_PRESSURE_FREE_CRIT_PCT` | `85` / `10` / `5` | `host-pressure.sh` thresholds. |
| `FFS_PRESSURE_MAX_WAIT_SECS` / `FFS_PRESSURE_DEFER_SECS` | `7200` / `300` | Pressure wait cap (then launch anyway) / reconcile defer window. |
| `FFS_PRESSURE_BACKOFF_BASE_SECS` / `FFS_PRESSURE_BACKOFF_CAP_SECS` | `60` / `900` | `host-pressure.sh wait`: sleep is base × 2^k seconds, capped (`host-pressure.sh:29-30,116-117`). |
| `FFS_CI_WATCH_BASE_SECS` / `FFS_CI_WATCH_CEILING_SECS` | `30` / `600` | `ci-watch.sh watch`: first poll interval in seconds, doubled after each poll up to the ceiling (`ci-watch.sh:32`). |
| `FFS_CI_WATCH_MAX_SECS` | `7200` | `ci-watch.sh watch`: total seconds before it stops with `CI-WATCH:deadline` and rc 75 (`ci-watch.sh:32`). |
| `FFS_HOST_PRESSURE` | `on` | `off` disables the pressure lever; `FFS_HOST_PRESSURE_FIXTURE=ok|high` overrides the reading for tests. |
| `FFS_RECONCILE_INTERVAL_SECS` | `300` | `reconcile-agent.sh` launchd `StartInterval`. |
| `FFS_RESPAWN_MIN_SECS` | `600` | Retry timeout floor, capped by the original timeout. |
| `FFS_CI_RERUN_MAX` | `2` | Durable CI rerun budget. |
| `FFS_CI_GH_ERR_MAX` | `5` | Consecutive GitHub API errors allowed before the lifecycle record fails. |
| `FFS_RECONCILE_STALE_SECS` | `300` | Dead launcher age before stale-running recovery is eligible. |
| `FFS_SESSION_WAKE` | `on` | Set to `off` to skip banner checkpointing in the runner's failed-drive path. |
| `FFS_SESSION_WAKE_MAX_SECS` | `21600` | Cap on how far ahead a parsed reset time may schedule the wake. |
| `FFS_SESSION_WAKE_MAX_ATTEMPTS` | `4` | Durable wake budget (`budgets.wakes`) seeded on first checkpoint. |
| `PLAN_WALL_AWAIT_POLL` | `15` | Maximum seconds between bounded wall-verdict probes. |
| `PLAN_WALL_AWAIT_MAX` | `6` | Pending `--await` returns allowed per phase before rc 76 `WALL-AWAIT:attempts-exhausted`; the counter resets on any decided outcome, so a decided wall always reports through. |

Look for `LIFECYCLE:`, `SESSION-WAKE:`, `CI-WATCH:`, `WALL-AWAIT:`, `RECONCILE:`, `GSD-RUN:RESPAWN`, `GSD-RUN:ENV-DEATH`, `DRIVE:`, `HOST-PRESSURE:` and `RECONCILE-AGENT:` lines. Normal no-ops use rc 0; lifecycle validation failures use rc 1; an unresolved wall-await returns rc 75 (rc 76 once `PLAN_WALL_AWAIT_MAX` pending returns are spent — checkpoint instead of polling further) and a decided blocked wall returns rc 20. A terminal record (`done`/`failed`/`quarantined`) reports `RECONCILE:terminal run=<id> state=<state>`, never `still-waiting`. A reconciler claim already held by another pass is deliberately a typed rc 0 no-op.

Schedule passes with the opt-in agent:

```bash
bash scripts/gsd/reconcile-agent.sh install --repo /path/to/consumer-repo   # macOS launchd, every 300s
bash scripts/gsd/reconcile-agent.sh status
```

On Linux the same command prints the equivalent cron line (`*/5 * * * * cd /path/to/consumer-repo && bash scripts/gsd/reconcile.sh`) and never edits crontab. FFS installs the scheduler only through that explicit opt-in. Per-mechanism sunset: the time-wake mechanism can be deleted when the vendor supplies durable, authenticated delayed-resume; the CI mechanism when CI completion webhooks reach the runner; the wall mechanism when plan-review callbacks exist; the respawn loop when the vendor runner reports and retries its own dead executors; the plan-length gate when plan review itself enforces a length budget upstream.

## ci-watch.sh

`scripts/gsd/ci-watch.sh` reads one GitHub Actions run through `gh run view`. It has two verbs. Both need `gh` and `python3`; any other invocation prints `CI-WATCH:usage` and exits 1.

```bash
bash scripts/gsd/ci-watch.sh evaluate --run-id ID
bash scripts/gsd/ci-watch.sh watch --database-id ID
```

- `evaluate` is one bounded probe of the run pinned in the lifecycle record (`wake_condition.params.databaseId`). `reconcile.sh` calls it for `ci-completed` wake conditions. It prints one line and never sleeps.
  - `CI-WATCH:pending`, `CI-WATCH:awaiting-attempt` and `CI-WATCH:gh-error idle` exit 0. A `gh` error counts toward `FFS_CI_GH_ERR_MAX`.
  - `CI-WATCH:pass` marks the record ready and exits 0.
  - `CI-WATCH:rerun:<id>` reruns the failed jobs (`gh run rerun --failed`). A new rerun happens only when the failed log matches an infrastructure signature (`runner-lost`, `system cancellation`, `startup_failure`, `network`, `dns`, `timeout`, `429`, `disk-space`) and the `FFS_CI_RERUN_MAX` budget allows it. A rerun already reserved by an earlier pass is issued without a new log check. `CI-WATCH:rerun-observed` clears a rerun that GitHub already accepted.
  - `CI-WATCH:rerun-exhausted` and `CI-WATCH:test-failure` exit 1. A test failure marks the record failed.
  - `CI-WATCH:missing-record` and `CI-WATCH:missing-database-id` exit 1.
- `watch` is a foreground poll loop on a run id you supply. `reconcile.sh` does not call it. It prints `CI-WATCH:poll databaseId=<id> interval=<secs>` before each poll, then ends with `CI-WATCH:pass` (rc 0), `CI-WATCH:test-failure` (rc 1) or `CI-WATCH:deadline` (rc 75). An infrastructure-signature failure prints `CI-WATCH:pending` and keeps polling; `watch` never reruns. The interval is bounded by the `FFS_CI_WATCH_*` controls above.

## Known issue: reconcile relaunch fails on bash 3.2

`scripts/gsd/reconcile.sh` uses `mapfile` (line 149). `mapfile` needs bash 4 or newer. The launchd agent runs the script as `/bin/bash scripts/gsd/reconcile.sh` (`reconcile-agent.sh`, `ProgramArguments`), and stock macOS `/bin/bash` is 3.2. On that interpreter the relaunch path fails after it has charged the budget:

1. `lifecycle.sh relaunch` runs first. It spends one unit of budget (`respawns` by default, `env_deaths` for a stale launcher) and writes the record as `running`.
2. `mapfile: command not found` goes to `.planning/run-state/reconcile-agent.log`. The `command` array stays unset, and under `set -u` the next line aborts the pass with `command: unbound variable`.
3. The runner is never launched. The pass stops, so later records are skipped, and this pass does not release its coordination claim for the run.
4. The record stays `running` with a dead launcher pid. Later passes report `RECONCILE:claim-held` and skip it until `scripts/coord/coord.py` lets the held claim go: when its holder is provably dead, or after its TTL (default 300 seconds). Once the claim is free and `FFS_RECONCILE_STALE_SECS` has passed, a pass treats the record as a stale launcher, charges `env_deaths`, and fails the same way. This repeats until the budget is spent and the record fails.

Any other caller that runs the script under bash 3.2 fails the same way, including a manual `bash scripts/gsd/reconcile.sh` when `bash` on your PATH is the system one. No fix has landed on this branch.

Workaround:

- On a host whose reconcile interpreter is bash 3.2, uninstall the launchd agent so it stops spending budget: `bash scripts/gsd/reconcile-agent.sh uninstall --repo /path/to/consumer-repo`. `install` always writes `/bin/bash`, so the agent cannot select a newer interpreter, and a hand edit of the generated plist is overwritten by the next `install`.
- Run the pass yourself under a bash 4+ interpreter, for example a package-manager install of bash: `<path-to-bash4>/bash scripts/gsd/reconcile.sh`. Check it first with `<path-to-bash4>/bash --version`.
- Or resume the run by hand. For a drive, run `bash scripts/gsd/drive.sh resume --run <run id>`. For any other run, read `resume_argv` from the run's lifecycle record in `.planning/run-state/` and run that argv from the repo root with `GSD_RUN_ID=<run id>` and `GSD_RESUME=1` set. `gsd-run.sh` writes its own launch record and pid when `GSD_RUN_ID` is set, so no extra step is needed. A record that a failed pass left `running` no longer shows its original wake reason, so set `GSD_RESUME=1` for it.

Budget already spent by a failed pass is not refunded.

## Evidence and boundary

Current evidence is n=1, so it is directional rather than a reliability guarantee. The spec-011 retro measures intervention-free completion rate and the wall-clock/active ratio.

Lifecycle and verdict records share the filesystem privilege of the gate code. Shape validation and the argv allowlist reduce cheap forgery, but a same-privilege writer is explicitly outside this threat model.
