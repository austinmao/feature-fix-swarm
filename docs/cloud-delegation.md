<!-- generated-by: gsd-doc-writer -->

# Cloud delegation

Opt-in offload of long, well-scoped coding tasks from `/swarm` to Claude Code
on the web and Codex cloud sessions. FFS pushes a task branch, starts a
provider session, watches for the result, checks it locally, and builds a
local integration branch for you to merge. This page is the operator guide
and the reference for contributors.

Status: experimental. The Claude backend has had live runs (2026-11-20,
Claude Code 2.1.329, one trivial docs task). The Codex backend has not run
live, and its output parsers are still unverified. See
[Live verification](#live-verification) for what was and was not checked, and
[Not in this release](#not-in-this-release).

## What it is and what it is not

`--cloud` is an opt-in flag on `/swarm` only. Both of these turn it on:

- `/swarm ... --cloud` or `--cloud=auto|prefer|required` (bare `--cloud`
  means `auto`).
- The `FFS_CLOUD` environment variable being present. Only the `/swarm`
  skill text reads it; its value is not mapped to a policy, so the policy is
  `auto` unless you pass the flag with a value.

What it does:

- Routes eligible tasks to a Claude cloud session or a Codex cloud task.
- Runs a detached, deterministic orchestrator that launches, polls, binds,
  collects, reviews, tests and integrates results.
- Leaves ineligible tasks to the normal local `/swarm` flow.
- Produces a local branch `ffs/cloud/<run>/integration`. Nothing is merged,
  nothing is deployed, and no branch of yours is touched.

What it is not:

- It does not use API keys. Both CLIs must be logged in with OAuth, and
  `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` must be unset.
- It is not available on `/feature-implement` or `/task-swarm` yet. Given
  `--cloud` (any form) or `FFS_CLOUD`, those skills print one line saying
  cloud delegation for them arrives in a later release and that
  `/swarm --cloud` is available now, then stop. Nothing else about them
  changes.
- It is not a way to run untrusted code safely. Cloud results are treated as
  untrusted input and gated, but a session you start still runs with
  whatever its environment exposes.
- It does not offload review or local tests. Those stay on your machine.

Without the flag nothing in the `/swarm` flow changes. The documented
exceptions are:

1. The operator-only guard hook gains deny patterns that apply to every
   session (see [Guard patterns](#guard-patterns)).
2. The installer ships the new files (see
   [Installer](#installer)).
3. The `/swarm` skill declares `network: true`.

The `autonomy-grant` and `preflight` skill texts also gained cloud notes that
do nothing unless cloud is used. The `/spec-status` facts output prints a
read-only `== CLOUD ==` section only when a cloud run ledger exists, so a
repository that never used `--cloud` sees no change there.

## Quick start

Prerequisites:

- macOS. Result tests run under `/usr/bin/sandbox-exec`. `doctor` fails when
  it is missing or when its startup self-check fails, so cloud stays off on
  other platforms in this release.
- Claude Code CLI and/or Codex CLI, logged in with OAuth. `doctor` checks
  that the binary is found and that no API-key variable is set. It also runs
  a best-effort login probe (`claude auth status`, `codex login status`) when
  the CLI lists that subcommand. A probe that is missing, fails or times out
  gives `WARN LOGIN_UNVERIFIED`, never a `FAIL`, so confirm the login
  yourself.
- A GitHub `origin` remote whose URL carries no credentials. A URL with
  embedded user and password or token is refused (`ORIGIN_HAS_CREDENTIALS`),
  by `doctor`, by `start` and when the orchestrator starts. Use a credential
  helper or SSH. Your current commit must be pushed and tracked files must be
  clean. Untracked files are allowed.
- The `gh` CLI, authenticated. `doctor` reads rulesets with `gh api`.
- Python 3, git and bash. Run from the main worktree, not a linked one
  (`NOT_MAIN_WORKTREE` otherwise).

Steps:

1. Write the consumer files. From the repository root:

   ```bash
   bash scripts/gsd/cloud-dispatch.sh env init --register-hook
   ```

   This writes `.ffs/cloud/config.json`, `setup.sh`, `env-check.sh` and
   `session-start.sh`. `--register-hook` also merges one `SessionStart`
   entry into `.claude/settings.json`. Existing files are kept unless you
   pass `--force`. Nothing is committed.

2. Edit `.ffs/cloud/config.json`. At minimum set `verify.cmd` to your test
   command. Tasks without a verify command always run locally. See
   [Config file](#config-file).

3. Commit and push `.ffs/cloud/*` and `.claude/settings.json`. `doctor`
   reads them from the commit, not the working tree.

4. Create the cloud environment for each provider you use. The command
   prints the one-time steps. It cannot create or verify the environment
   for you.

   ```bash
   bash scripts/gsd/cloud-dispatch.sh env-spec --provider claude
   bash scripts/gsd/cloud-dispatch.sh env-spec --provider codex
   ```

   For Codex, export the environment id in the shell that runs the
   dispatcher: `export FFS_CODEX_CLOUD_ENV=<environment id>`. See
   [Cloud environment setup](#cloud-environment-setup).

5. Claude only: accept the folder trust dialog once. `claude --cloud` shows
   a trust dialog for the folder it is started in. Trust is per git root, so
   a trusted parent directory does not cover a new clone. FFS never answers
   the dialog. A launch that meets it ends `failed` with
   `CLI_EXIT BLOCKED_DIALOG` and creates no session. Without
   `FFS_CLOUD_CLONE_PARENT` the launch clone is a fresh temporary directory,
   which always shows the dialog. Do these in order:

   1. Choose a directory outside the repository that you own and that is not
      a symlink. Export it in the shell that runs the dispatcher. This fixes
      the launch clone path at `<parent>/ffs-cloud-launch-claude`:

      ```bash
      mkdir -p <parent>
      export FFS_CLOUD_CLONE_PARENT=<parent>
      ```

   2. Clone your repository to that exact path. The directory must exist as
      a git clone when you trust it:

      ```bash
      git clone <origin url> <parent>/ffs-cloud-launch-claude
      ```

   3. In a real terminal, run the interactive client in that directory,
      accept the trust dialog, then quit:

      ```bash
      cd <parent>/ffs-cloud-launch-claude && claude
      ```

   4. Optional check. This shows the client's first screen and creates no
      session. `dialog` should be `false`:

      ```bash
      bash scripts/gsd/cloud-dispatch.sh probe-claude --dir <parent>/ffs-cloud-launch-claude
      ```

   FFS deletes this clone and makes a fresh one at the same path for each
   launch. The live run launched without a dialog after these steps.

   FFS creates the clone directory itself, in a private staging directory
   inside `<parent>` (names starting `.ffs-cloud-new-`, mode 0700). It records
   the directory in an ownership marker file beside the final path,
   `<parent>/ffs-cloud-launch-claude.ffs-owned`, holding the directory's device
   and inode. The marker is written to a temporary file in `<parent>` (also
   starting `.ffs-cloud-new-`) and renamed into place without replacing anything,
   so its name never holds a partial marker and an existing file there is never
   overwritten. FFS then publishes the directory at
   `<parent>/ffs-cloud-launch-claude` with a rename that fails if the path
   exists (a libc without the kernel's no-replace rename falls back to a check
   followed by a plain rename). It then clones into that directory. To delete it (on
   exit, and when it finds one left by a killed launch) only when the marker
   names that very directory, so a clone killed at the transfer timeout is
   cleaned up by the next launch. FFS first renames the directory into a private
   staging directory (`.ffs-cloud-old-…`) and compares its device and inode with
   the marker there; a mismatch is put back or left in staging, never deleted.
   A launch killed part-way leaves only such staging directories, which the next
   launch removes only when they are provably its own (its prefix, mode 0700,
   owned by you). A `.ffs-cloud-new-*` directory is removed only when it is
   empty (`rmdir`); a retired clone in a `.ffs-cloud-old-*` directory is deleted
   only if the marker names it. The next launch also removes a marker temporary
   file that holds a valid marker or nothing, and an empty marker left beside a
   clone that is gone. A non-empty `.ffs-cloud-new-*` directory, such as one a
   killed clone left behind, is kept: remove it by hand once you have checked it.
   Anything else is left. Your hand-made clone has no marker, so the
   first launch ends `CLONE_PATH_INVALID` and touches nothing. **One-time
   operator action:** remove your clone
   (`rm -rf <parent>/ffs-cloud-launch-claude`) after you have accepted the
   trust dialog; the next launch recreates it with the marker. This also
   applies to a clone left by an earlier release, which carried its marker
   inside `.git/` and is no longer recognised. The path must also not equal,
   contain or lie inside your checkout or any of its linked worktrees
   (compared case-insensitively); otherwise the launch ends
   `CLONE_PATH_INVALID`. A directory that appears at the path while a launch
   runs is never deleted or marked. A file at the marker path that is not a
   marker FFS wrote (other content, a second link, a symlink, a FIFO) ends the
   launch `CLONE_PATH_INVALID MARKER_UNRECOGNIZED` (found while clearing a stale
   marker) or `CLONE_PATH_INVALID MARKER_EXISTS` (found while creating the
   marker) and is never removed or truncated. If the directory at the path is no
   longer the one the marker names when FFS retires it, the launch ends
   `CLONE_PATH_INVALID MOVED_AWAY` and the directory is put back, not deleted.

6. Run the checks:

   ```bash
   bash scripts/gsd/cloud-dispatch.sh doctor
   ```

   Exit 0 means no `FAIL`. Fix every `FAIL`. `WARN` lines are advisory. If
   it reports `HISTORY_SECRET`, see
   [Doctor and history findings](#doctor-and-history-findings).

7. Preview the routing. Annotate tasks as described in
   [Task annotations and routing](#task-annotations-and-routing):

   ```text
   /swarm --cloud --dry-run \
     "Add retry with backoff to the HTTP client [owns: src/client/**] [est: 45m] [verify: npm test -- client]" \
     "Reword the README intro [owns: README.md] [est: 5m] [verify: true]"
   ```

   The dry run runs `doctor` and `plan`, prints groups, routes, providers,
   reasons and missing grants, and stops. It starts nothing.

8. Run it for real by dropping `--dry-run`. In an interactive session
   `/swarm` shows the task list, providers and every exposed secret name and
   asks one question. On yes it records the grants (see [Grants](#grants)).
   Autonomous and headless runs never grant for themselves; they need grants
   that already exist, and `start` refuses when one is missing.

9. Watch progress. The `/swarm` session reports the run id.

   ```bash
   bash scripts/gsd/cloud-dispatch.sh status --run <run>
   ```

   Exit 4 means at least one task needs you (`needs-operator` or
   `uncertain`).

10. Merge the integration branch yourself, after reviewing it:

   ```bash
   git log --oneline ffs/cloud/<run>/integration
   git merge --no-ff ffs/cloud/<run>/integration
   ```

   Then clean up with
   `bash scripts/gsd/cloud-dispatch.sh cleanup --run <run> [--remote]`.
   `--remote` does not delete the `claude/ffs-*` branch that the Claude
   worker created. Delete it by hand. `cleanup` also refuses a run that is
   not `done`. A run whose task never bound a result stays `active` until
   you `abandon` that task.

### Doctor and history findings

For Claude, `doctor` scans the full ancestry of your base commit with the
outbound rules, because Claude's transport may bundle the checked-out
branch. In this release:

- Any binary file in history is a `binary-blob` finding. Allow it in
  `config.scan.allow` with `{"blob": "<oid>", "class": "binary-blob"}`. The
  blob id is in the finding location (`blob:<oid>:<path>`).
- Any file named like `.env*`, `id_rsa*` or `*.pem` in history, including
  an innocent `.env.example`, is a `secret-filename` finding. Blob ids do not
  match these findings, because they carry a path id. Allow them by path:
  `{"path": "<repo path>", "class": "secret-filename"}`. A path entry applies
  to one finding class at one path. Setting `limits.claude` to `0` skips the
  check and leaves Codex only.

- The `pem-block` class flags private-key armor only: a block whose label
  contains `PRIVATE KEY` or `SECRET KEY` (`RSA`, `EC`, `DSA`, `OPENSSH`,
  `ENCRYPTED`, bare, `PGP PRIVATE KEY BLOCK` or `PGP SECRET KEY BLOCK`). Signature blocks (`PGP SIGNATURE`,
  `SSH SIGNATURE`), public keys and certificates are not findings. Signed
  commits and tags carry a signature block in their `gpgsig` header, so the
  scan reads commit messages and headers without flagging the signature. A
  private key pasted into a commit message is still found, signed or not.
- A finding inside a commit or tag object (`commit:<oid>:L<line>`) has no
  blob id and no path, so `scan.allow` cannot allow it. Rewrite the history,
  or set `limits.claude` to `0`.

`scan.allow` is read from `.ffs/cloud/config.json` **in the base commit**
(`HEAD`), not from the working tree. An allowlist you have not committed has
no effect, and `doctor` says so. Commit the file, then rerun `doctor`.

The `doctor` line for `HISTORY_SECRET` shows the first 20 findings and a
count (`N findings, showing first 20; use doctor --json for all`).
`doctor --json` carries every finding in `checks[].data.findings` for the
`HISTORY_SECRET` check: `class`, `kind` (`blob`, `commit`, `tag`, `tree` or
`path`), `oid`, `path`, `line` and, where one can allow it, the ready-made
`allow_entry`. It never carries the matched value. `allow_entry` is `null`
when no entry can name the real finding: commit and tag objects, a path that
contains unprintable or secret-shaped bytes (shown as `<unsafe-path>`), and a
path too long to show in full.

Each scan cache entry carries the signature of `config.scan.allow` and the
scanner version. If you change the allowlist, or a release changes a pattern
class, the next scan rescans the full history once. The scanner version is now
4: `pem-block` also flags `SECRET KEY` armor (`PGP SECRET KEY BLOCK`), so
verdicts cached by version 3 are rescanned.

A shallow checkout cannot be scanned: a local walk stops at the shallow
boundary, while the launch clone from origin includes the older history. The
history scan refuses with `shallow-repository` (`HISTORY_SECRET` fails in
`doctor`; start and publish stop) and writes no cache entry. Run
`git fetch --unshallow` first.

## Task annotations and routing

### Annotations

Put these in square brackets anywhere in the task text. Unknown brackets are
left in the title.

| Annotation | Meaning |
|---|---|
| `[owns: a/**, b.py]` | Repo-relative globs the task may change, comma separated. Required for the cloud. `**` spans directories, `*` does not. A glob also covers everything below it. |
| `[est: 45m]` | Estimate. Accepts `45m`, `2h`, `1h30m` or bare minutes (`90`). Drives the threshold. Missing means 0. |
| `[verify: cmd]` | Test command run in the sandbox. May contain balanced brackets. Falls back to `config.verify.cmd`. |
| `[needs-secret: NAME ...]` | Secret names the task uses, separated by commas or spaces. Use `UPPER_SNAKE` names. |
| `[local]` or `[local: reason]` | Never offload this task. |
| `[after: t1 t2]` | Task ids this task depends on. |
| `[id: x]` | Explicit task id, `^[a-z0-9][a-z0-9-]{0,23}$`. Default is `t1` to `tN` in input order. |

If `owns` or `est` is missing, the `/swarm` session estimates them and marks
`owns_source` as `estimated`. A wrong `owns` estimate stops the result at
`path-outside-owns`, so annotate `owns` yourself. `verify` is never
invented.

When you call `plan` or `start` directly, the tasks file is a JSON list (or
an object with a `tasks` list) of strings with annotations, or of objects.
Object fields that have no annotation form: `tier`
(`volume|execution|judgment|frontier`, default `execution`),
`model_request` (`{"kind":"tier","name":"..."}` or an exact request),
`kind` (`build|review|verify|search`, default `build`), `prompt`, `title`.

### Eligibility

A task can go to the cloud only if none of these hold. Reason codes appear
in `plan` output under `tasks.<id>.reasons`.

| Reason code | Task is ineligible when |
|---|---|
| `LOCAL_ONLY` | It has `[local]`. |
| `KIND_NOT_CLOUDABLE` | Its `kind` is `review` or `verify`. |
| `MODEL_EXACT` | It requests an exact model id. |
| `TIER_FRONTIER` | Its tier is `frontier`. |
| `OWNS_MISSING` | It has no `owns`. |
| `OWNS_INVALID` | An `owns` glob is absolute, contains `..`, or is empty. |
| `OWNS_DENYLIST` | An `owns` glob could name a denylisted path. See [Denylist](#denylist). |
| `VERIFY_CMD_MISSING` | It has no `[verify]` and `config.verify.cmd` is empty. A whitespace-only command counts as empty: `bash -c '   '` exits 0 and would pass with no test run. |
| `NO_PROVIDER_AVAILABLE` | No provider is usable: binary missing, API-key variable set, Codex environment id missing, or the provider limit is 0. |
| `NO_PROVIDER_FOR_TIER` | No usable provider takes its tier. Claude takes execution, volume and judgment. Codex takes execution and volume. |
| `SECRET_NOT_EXPOSED` | A usable provider for its tier does not list all its `needs-secret` names in `secrets.exposure.<provider>`. |

### Groups and thresholds

Tasks are grouped as connected components of the dependency graph. Two
tasks whose `owns` overlap get an implicit dependency edge in input order,
so they serialize. A group is a unit: it goes to the cloud only if every
task in it is eligible and at least one task has an estimate at or above
the threshold. Otherwise the whole group runs locally.

| Policy | Threshold | If a group stays local |
|---|---|---|
| `auto` | 20 minutes | Runs locally. |
| `prefer` | 5 minutes | Runs locally. |
| `required` | 0 (eligibility alone decides) | `plan` and `start` exit 3 with `CLOUD_REQUIRED_NOT_ELIGIBLE` and launch nothing. |

When `host-pressure.sh check` reports high pressure (exit 75), the
threshold for `auto` drops to 5 minutes.

Group-level reason codes for local groups: `POLICY_OFF`, `BELOW_THRESHOLD`,
`GROUP_HAS_INELIGIBLE_TASK`. A task that is eligible but sits in a local
group gets `GROUP_LOCAL`. Cloud tasks get `CLOUD_ELIGIBLE`.

Run-level refusals keep every task local. `plan` reports them as reason
codes, and `start` exits 3 with the short name:

| `plan` reason code | `start` refusal | Cause |
|---|---|---|
| `RUN_LIFECYCLE_DISABLED` | `LIFECYCLE_DISABLED` | `FFS_LIFECYCLE=off`. |
| `RUN_MANAGED_INGRESS` | `MANAGED_INGRESS` | `FFS_MANAGED_INGRESS` set and not `0`. |
| `RUN_TOKEN_BUDGET` | `TOKEN_BUDGET` | `GSD_TOKEN_BUDGET` set. |
| `RUN_DIRTY_BASE` | `DIRTY_BASE` | Tracked files modified. |
| `RUN_DOCTOR_NOT_GREEN` | `DOCTOR_NOT_GREEN` | `plan --with-doctor` found a `FAIL`. `start` always runs `doctor`. |

### Provider choice

For each task in a cloud group the candidates are the usable providers that
take its tier and whose `exposure` covers its `needs-secret` names. FFS picks
the one with more free slots and alternates on ties, starting with Claude.
Free slots come from `config.limits` minus the slots held by all runs.

Claude receives `--model` and `--effort` from the task's tier request.
Codex has no model flag, so the model is whatever the Codex environment
defaults to. The plan marks these `requested-unverified` and
`provider-default-unverified`.

### What always stays local

- Review and verification tasks. Reviews must come from the opposite model
  family on your machine.
- Tasks that need an exact model, or the frontier tier.
- Tasks without a declared, safe `owns`, or without a verify command.
- Tasks whose secrets no provider environment exposes.
- Anything marked `[local]`, including tests that need network access or
  live services. Sandboxed tests have no network.
- Everything when a run-level refusal applies.
- Mixed groups. If one task in a connected group cannot go to the cloud, the
  whole group runs locally. Under `auto` and `prefer` that is a quiet
  fallback. Under `required` it is a refusal.

## The orchestrator

The orchestrator is one Python process per run (`cloud_dispatch.py
orchestrate`, started through `cloud-dispatch.sh`). It is deterministic and
contains no LLM. The only model calls in the pipeline are the cross-family
review step, which runs through `scripts/gsd/cloud-review.sh`.

`start` writes the run ledger and spawns the orchestrator detached, in its
own session, with output going to `runs/<run>/orchestrate.log`.
`start --foreground` runs it in the calling process instead.

Startup, in order:

1. Take the exclusive lock `runs/<run>.orch.lock`. If another process holds
   it, print `CLOUD:orchestrator-running pid=<pid>` and exit 0. One
   orchestrator per run. With `--wait-lock SECONDS` the process waits for the
   lock instead, and exits 75 if the lock is still held after that time.
2. Check that `origin` still has the URL pinned at `start`
   (`ORIGIN_CHANGED` otherwise) and that the URL still carries no
   credentials (`ORIGIN_HAS_CREDENTIALS` otherwise).
3. Load `runs/<run>/tasks.json` and check its sha256 against the ledger
   (`TASK_INPUTS_CHANGED` otherwise). Prompts live only in that 0600 file,
   never in the ledger.
4. Register the lifecycle record `cloud-<run>-e<epoch>` with
   `scripts/gsd/lifecycle.sh`. The resume command is
   `scripts/gsd/cloud-dispatch.sh orchestrate --run <run> --root <abs>
   --resume`, with budgets `respawns 0` and `env_deaths 8`. If lifecycle
   is disabled the run is refused. If the record is terminal, the epoch is
   bumped once and registration is retried.
5. If the run was parked, resume it. A task found in `submitting` becomes
   `uncertain` on the next heartbeat (the first step of every tick). FFS
   never relaunches it automatically.

Each tick runs these phases and then sleeps:

| Phase | Work |
|---|---|
| Replay | Update the heartbeat (a task left in `submitting` becomes `uncertain` here). Finish unfinished journal entries (`reserve`, `release`, `admit`, `integrate`, `park`). |
| Expire slots | Release the provider slot of every `needs-operator` or `uncertain` task whose launch deadline has passed. Runs again before a park. |
| Poll and bind | Poll `launched` and `running` tasks, apply the deadline, bind results. |
| Collect | Collect, review and test results. At most `limits.max_local_verifiers` tasks per tick (default 1). |
| Integrate | Move verified results onto the integration ref. |
| Admit | Push task branches for tasks whose dependencies are integrated. |
| Launch | Reserve provider slots, re-check grants, submit. |
| Finish | Mark the run `done`, or park it. |

The sleep is 60 seconds while any task is in flight, otherwise it doubles up
to 600 seconds, with plus or minus 10% jitter.

Every multi-step action writes a journal entry first, acts, then marks it
done. After a crash the next tick replays unfinished entries. Every result
carries the task generation, and a result for an old generation is dropped.

### Lifecycle and restarts

The registered lifecycle record lets `scripts/gsd/reconcile.sh` restart a
dead orchestrator as `orchestrate --resume`. See [healing](healing.md). Three
limits apply:

- Reconcile runs only when something invokes it: the optional reconcile
  agent (`scripts/gsd/reconcile-agent.sh install`), or an FFS entrypoint.
  Without either, a dead orchestrator stays dead until you restart it.
- The repository's `reconcile.sh` uses `mapfile`, which bash 3.2 (the macOS
  system bash) lacks. `doctor` warns (`RECONCILE_MAPFILE`). Then restarts
  need a manual `--resume`.
- A restart by reconcile does not inherit variables you exported in your
  interactive shell, such as `FFS_CODEX_CLOUD_ENV`, `FFS_CLOUD_CLONE_PARENT`
  or `DIGEST_NOTIFY_CMD`.

### Park and resume

When no task can progress and at least one is `needs-operator` or
`uncertain`, the orchestrator parks the run. It journals `park`, sets the run
state to `parked`, prints and logs one line, and exits 0:

```text
CLOUD-PARKED run=<run> items=<n> see: cloud-dispatch.sh status --run <run>
```

If `DIGEST_NOTIFY_CMD` is set, the same line is piped to
`sh -c "$DIGEST_NOTIFY_CMD"`. A parked run has no process and no restart
budget in use.

`adopt`, `abandon` and `resolve` change the ledger, bump a wake counter,
and then make sure an orchestrator will see the change. If the lock is free
they start a detached `orchestrate --resume`. If the lock is held, they start
a detached `orchestrate --resume --wait-lock 300`, which waits up to 300
seconds for the holder to exit and then runs. If the run is `done` they
print `CLOUD:orchestrator-not-needed` and start nothing. The park handshake
checks the wake counter, so a change made while the orchestrator decides to
park prevents the park, and the orchestrator checks it once more just before
it exits.

### Supervision limits

- The orchestrator runs on your machine. When the machine sleeps or shuts
  down, polling stops. Cloud sessions keep running. Results wait on the
  provider side and are picked up when the orchestrator runs again.
- The deadline is wall-clock time from launch (`deadline_hours`, default
  12). A sleeping host still spends it.
- For Claude there is no failure signal. A session that dies without
  pushing a branch is noticed only when the deadline passes.
- Nothing watches the orchestrator except reconcile, and reconcile has the
  limits above.

## Result handling

### Binding

Binding decides which provider output is the result of which task.

Claude: the prompt asks the worker to push to exactly
`claude/ffs-<run>-<task>-<nonce8>` and to write `.ffs-result/<task>.json`
carrying the full 32-hex nonce. FFS snapshots `refs/heads/claude/*` on
origin before launch, then polls with `git ls-remote`. The candidate pool is
the first non-empty set of these: the exact requested name; refs containing
the 8-character nonce prefix; refs new or changed since the snapshot. Refs
already rejected for this task, and refs named for another task of the same
run, are never in the pool. More than one distinct candidate in the pool
means `ambiguous-candidates`. A result binds when the same OID is seen on
two consecutive polls. The first bind wins, and later candidates are
ignored. Every ref name is run through the secret scanner first. A ref whose
name matches is never snapshotted, bound or recorded by name (the launch
snapshot keeps only a hash of it). A new one, or one that carries the nonce,
parks the task (unless its own exact or nonce-named result is already among
the clean candidates) as `needs-operator` / `ambiguous-candidates` with the fixed
reason "a remote claude/* ref name matched the secret scanner; name
withheld". Find the ref on origin yourself and use `adopt --oid` or `abandon`.
A scanner fault is not a verdict on any ref name: at launch it ends
`SNAPSHOT_FAILED` (nothing is started), and a poll that hits it reports
`unknown` (`REF_SCAN_REFUSED`) and tries again on the next tick. If a ref bound from polling has a missing or invalid manifest, FFS
treats it as another session's result: it records the ref as rejected,
clears the bind and returns the task to polling. A result you bound with
`adopt` is not treated that way. There is one exception: a ref that is
provably this task generation's own result. That is a ref named exactly
`claude/ffs-<run>-<task>-<nonce8>`, or a manifest carrying this generation's
nonce, whose manifest is present but fails validation. FFS moves that task
to `needs-operator` with blocker `manifest-invalid` and stops polling. The
recorded detail names the validator's error code (for example
`MANIFEST_TESTS_RUN`), never text the worker wrote. A live run hit this once,
when the worker wrote `tests_run` items as strings. Only `abandon` clears the
blocker; `abandon --retry` starts a new generation. A foreign `claude/*` ref
that merely is new, and a branch with no manifest at all, still go back to
polling.

Codex: there is no branch. FFS polls `codex cloud status <id>` and, when the
task is ready, downloads `codex cloud diff <id>`, applies it to the task base
in a quarantine repository and commits it as `FFS Cloud Dispatcher`. The diff
is the binding. If the diff contains `.ffs-result/<task>.json` it is
validated like a Claude manifest, nonce included. If not, FFS synthesizes a
`complete` manifest.

### Collect pipeline

For each bound result, in this order. The first failing step decides the
outcome.

1. Fetch into a quarantine repository with no remotes. The fetch uses a
   5 MiB blob size filter.
2. Scan every new object (all blobs, commit messages, paths) and the raw
   manifest bytes for secret-shaped content. A hit rejects the result with
   `secret-leak` and prints the [runbook](#runbook-secret-leak). This runs
   before any other check and reports even if a later check would also fail.
3. Structure checks: the result descends from the task base; at most 1000
   commits; no symlink (mode 120000) or submodule (160000); every blob is
   present and at most 5 MiB.
4. Manifest: exact key set, matching `run`, `task`, `generation`, `nonce` and
   `base_sha`, string and list limits. Each `tests_run` item must be an
   object `{"cmd": "<command>", "rc": <integer exit code>}`; the worker
   contract in the packet says so. `status: blocked` becomes
   `manifest-blocked`.
5. Paths: every added, modified, deleted and renamed path (both sides) must
   match `owns` and must not match the [denylist](#denylist). The check runs
   on the net diff and on every commit of the result, so a path touched by one
   commit and restored by a later one still counts. A path that holds a
   Unicode format or control character (category Cf or Cc, for example a
   zero-width character) is refused as `denylist`, because some filesystems
   ignore such characters when they compare names. Paths that collide with
   each other or with an existing path once case is folded are also reported
   as `denylist`, with the detail `case-colliding paths`. There is no
   separate `case-collision` blocker.
6. Dependency gate: a change to a dependency or lock file stops the result
   at `dependency-change` until you decide. A change to a parsed manifest that
   touches no dependency declaration and no install-time key (for example only
   `version` in `package.json`) passes without a stop. File names are matched
   case-insensitively (`Package.json` is a manifest): a case-insensitive
   checkout opens it as `package.json`. See the
   [runbook](#runbook-dependency-change).
7. Candidate: FFS builds one new commit from the validated tree minus
   `.ffs-result/<task>.json`. Its only parent is the task base commit, and it
   has a fixed author, date and message. The worker's own commits are
   dropped, so nothing the gates did not validate is exported later. FFS
   imports it as `refs/ffs/cloud/<run>/cand/<task>-g<N>`. From here the
   candidate commit and its tree are frozen. Review, tests and integration
   all use exactly it.

### Review by the opposite model family

`scripts/gsd/cloud-review.sh <run> <task> <producer> <diff> <summary>
<generation> [attempt]` asks the other family to review: Codex reviews a
Claude result, Claude reviews a Codex result. It forces the cross-vendor
fallback off, so a failed reviewer can never be replaced by the producer's
own family. It requests the `judgment` tier with a 480 second limit. The diff
and all worker text go into an untrusted-data fence, and worker lines that
start with `VERDICT:` are prefixed so they cannot read as a verdict. The
dispatcher starts the script with an allowlisted environment that carries no
API keys.

Before the diff is sent, FFS redacts secret-shaped content in its deleted and
context lines, because those come from your own base. A secret-shaped value
on an added line, or in the review prompt text, stops the result before the
reviewer sees it. The task ends as `rejected` with `secret-leak`, the detail
carries the leak findings, and `status` prints the runbook. Follow
[Runbook: secret leak](#runbook-secret-leak). A diff that changes binary
content always stops at `binary-change`, because the reviewer cannot see the
bytes. There is no allowlist for binary files. (The `review.binary_allow`
config key was removed.)

The verdict is the last non-empty line of the reviewer output, exactly
`VERDICT: APPROVE` or `VERDICT: REVISE`. When the reviewer ran, the script
first prints one `CLOUD-REVIEW-MODEL: requested=<model> answered=<model>
descent=<0|1>` line, taken from `adversary-host.sh`'s own record of which
model answered (not from the reviewer's output). It ends with one line
`CLOUD-REVIEW: APPROVE|REVISE|UNAVAILABLE|cap` and exits 0, 1, 3 or 4. Any
other verdict, a nonzero reviewer exit, or a diff over 2,000,000 bytes is
`review-unavailable`. An `APPROVE` only counts when the model that answered
is the requested judgment-tier model. `adversary-host.sh` can descend to a
weaker model of the same vendor and still return `APPROVE`. That approval ends
in `needs-operator` with `review-model-downgraded`, and an approval whose model
is not reported ends with `review-model-unknown`. The opposite-vendor rule and
`FFS_CROSS_VENDOR_FALLBACK=off` are unchanged. The ledger `review` record
carries `model` and `requested_model` (records written before this still load).
An approval that lacks both is not trusted when a run resumes: a task found in
`reviewed` or `verified` goes back to `collected` (a fresh review round meter)
and is reviewed and verified again, and an unfinished integration of such a
task is re-planned instead of applied, so the ref never moves on it. `REVISE` rejects the result. Each run of the script
spends one of two review rounds from a durable round meter for that task
generation, whatever the outcome. `resolve --action retest` on a review
blocker starts a fresh two-round meter (the optional `attempt` argument). It
is allowed twice per task generation. The third attempt is refused, and you
reject the task or `abandon --retry`. See
[Runbook: review unavailable](#runbook-review-unavailable).

The script is resolved only from the dispatcher's own install location:
`<lib dir>/scripts/gsd/` (the managed lib,
`~/.claude/lib/feature-fix-swarm/scripts/gsd/`), then
`<lib dir>/../scripts/gsd/` (a source checkout). It is never taken from the
target repository's `scripts/gsd/`, so a merged result cannot change later
reviews. The same rule applies to the other scripts the dispatcher runs
(`lifecycle.sh`, `reconcile.sh`, `reconcile-agent.sh`, `host-pressure.sh`).
`cloud-review.sh` sources
`adversary-host.sh` and `fence-data.sh` from its own directory.
`adversary-host.sh` sources `run-bounded.sh` and loads `lib/gates.py` and
`lib/model_requests.py` from two directories above the script. The installer
ships all of these files to the managed lib. `doctor` fails with
`REVIEW_UNAVAILABLE` when `cloud-review.sh` or `adversary-host.sh` cannot be
found. It does not check the other files, so a partial install shows up as
`review-unavailable` on a result.

### Sandboxed tests

The candidate tree is materialized without git filters and tested with
`config.verify.cmd` (or the task's `[verify]`) under `sandbox-exec`.

| Allowed | Denied |
|---|---|
| Fork and exec, so ordinary test runners work. | All network access. |
| Writes inside the temporary work tree and temporary directory, plus `/dev/null`, `/dev/dtracehelper` and `/dev/fd`. | Every other write. |
| Reads inside the work tree and the run's own temporary directory, the dependency roots in `verify.deps`, toolchain roots found on `PATH` under your home (see below), and `verify.read_roots`. | Reads of your home directory, `/Volumes`, the main worktree, every other worktree and the FFS state directory. Credential locations under your home are never readable (a root that contains one is refused, and the deny is applied again after the allow rules): `~/.ssh`, `~/.aws`, `~/.config`, `~/.codex`, `~/.claude`, `~/.doppler`, `~/.gnupg`, `~/.netrc`, `~/.docker`, `~/.kube`, `~/.npmrc`, `~/.pypirc`, `~/.orbstack`, `~/.git-credentials`, `~/.gitconfig`, `~/.pgpass`, `~/.azure`, `~/.vercel`, `~/.railway`, `~/.wrangler`, `~/Library` and the Cargo credentials files. |
| Mach lookup of one service, `com.apple.system.opendirectoryd.libinfo`, which user and group lookups need. Signals and process info only between processes inside the same sandbox. | `/tmp`, `/private/tmp`, `/var/folders`, `/private/var/folders` and the directory that holds the run's temporary directory (normally your `TMPDIR`), except the run's own temporary directory. All other Mach lookups, Launch Services open, Mach registration, IOKit open, system sockets, Apple events and job creation. |

Toolchain roots are narrow. A `PATH` entry under your home is granted as the
bin directory itself, never its parent. When the directory sits in a known
toolchain layout (`~/.nvm/versions/node/<v>`, `~/.pyenv/versions/<v>`,
`~/.rustup/toolchains/<t>`, `~/.asdf/installs/*/*`, uv, pipx and mise
layouts, and similar), the root of that layout is granted instead. A
symlink in such a directory is followed, and its target is granted only when
the target lies inside a known layout. `~/.cargo/bin`
grants only itself and the toolchains in `~/.rustup/toolchains`, not
`~/.cargo`. `~/.local/share` and `~/.cache` are never granted as a whole.
A root that is or contains your home, a worktree, the state directory, a
credential location or a temporary area is dropped (`PATH` roots) or refused
with `sandbox-unavailable` (`verify.deps` and `verify.read_roots`).

The command runs under `/bin/bash -c` with a clean environment: `PATH`
(entries under your home are kept only if they fall under a granted root;
entries under `/Volumes`, a temporary area or a worktree are dropped),
`HOME` and `TMPDIR` pointing into the temporary directory, `LANG`, `LC_ALL`
and `CI=1`. The child also gets `RLIMIT_CORE` 0, a 512 MB file size limit, 4096
open files, and a process limit of your user's current process count plus 512.

Output is collected through a writer that holds at most 8 MB, redacts
secret-shaped content, and writes the 0600 log once to
`runs/<run>/verify-<task>-g<N>.log`. A truncated log ends with
`[log truncated]`. If the scanner fails, the log is replaced by a notice that
it was withheld. The wall limit is `verify.timeout_s` (default 900). On
timeout the process group is killed and the result is `tests-failed`.

While the command runs, FFS polls the process table and records every
descendant of the launched process (by pid and start time). When the command
ends, FFS kills only the launched process, the descendants it observed, the
members of their process group or session, and the descendants of those. It
checks pid and start time again in a fresh process listing right before each
signal. It does not select processes by argument list or working directory.

Before it runs any test, FFS runs a startup self-check. The IPC rules must be
accepted by `sandbox-exec`, a signal to a process outside the sandbox must
fail, and `/usr/bin/pbpaste`, when it exists and works outside the sandbox,
must fail inside it. If the profile or the self-check fails, the sandbox is
treated as unavailable and the task stops at `sandbox-unavailable`. It does
not run without the rules.

Known residual gap: the sweep depends on the process-table polls. A
double-fork child that no poll saw while its parent chain was alive is not
swept. The sandbox rules still apply to it, but it is not killed.

Operator terminals (`/dev/tty*`) are not readable inside the sandbox.

Residual risk: one Mach service, `com.apple.system.opendirectoryd.libinfo`,
stays allowed, because user lookups (`getpass`, Node `os.userInfo()`, the git
author identity) need it. Whether it can carry DNS lookups for outside names
is not tested.

Things that follow:

- Tests that need network access or live services fail. Mark those tasks
  `[local]`.
- Dependencies come from `verify.deps` in your main worktree, linked in
  read-only. Install them locally first.
- The whole base tree is materialized. A tracked symlink or submodule
  anywhere in the repository makes the sandbox refuse, and every task ends at
  `sandbox-unavailable`.
- Exit code 0 is `verified`. Anything else is `tests-failed`.

### Integration

The target is the local branch `ffs/cloud/<run>/integration`, which starts
at the base commit. The candidate's only parent is the task base, so a
candidate whose base is the current tip is fast-forwarded.

Otherwise FFS merges only when the tip and the candidate changed disjoint
paths since their common ancestor. Paths are compared with case folded, and a
path counts as overlapping with its parent and child paths. The merge is built
from a temporary index. It uses no worktree, runs no hooks and does no content
merge. The merged tree must equal the tip with exactly the candidate's changed
paths taken from the candidate. If the two sides overlap, or the tree is
anything else, the task stops at `merge-conflict` with the paths listed.
FFS does not try to resolve the overlap. If two results would collide once
case is folded in the merged tree, the task also stops at `merge-conflict`,
with the detail `case-colliding paths`. The merged change must also have the
same `git patch-id` as the reviewed change (`patch-id-mismatch` otherwise).
The hash is computed with `--verbatim`, or with `--stable` on a git that lacks
that flag. Tests run again on the merged tree, and the digest of that retest log
is written into the integration journal with the intent, so a replay after a
crash records the merged tree's evidence, not the candidate's own log. A merge
does not get a second
review, because the merged change is identical to the reviewed one.

Then `git update-ref` moves the ref with a compare-and-swap on the old tip.
If the ref moved outside FFS before this step, the task stops at
`merge-conflict` with a note to reconcile the ref. If the compare-and-swap
fails because the ref moved in between, FFS plans the merge again.

After a crash, FFS settles an unfinished integration from the ref. It
accepts only two kinds of commit: the exact commit of the open intent (the
publish commit it journaled), and a commit the run ledger recorded as
published earlier. An intent alone is not proof of publication. If the ref
still holds the old tip, FFS applies the compare-and-swap again. A ref tip on
top of the journaled commit that is not recorded as published is
`integration-ref-foreign`, and FFS never adopts it, so no successor can start
from it.
If the ref already holds the journaled commit but the task's approval has no
reviewer-model evidence (an older process moved the ref), FFS does not record
the move: that would release the dependents. It parks the task at
`merge-conflict`. Reset the integration ref to the tip the ledger holds, then
run `resolve --action retest`, and the task is reviewed and verified again.

A task with dependencies starts from the integration tip. FFS pushes that
tip to origin only as the new task branch of the successor, after an
outbound scan and a grant check.

FFS never merges into your branch and never touches your working tree. The
integration ref stays local until you merge it.

## Reference

### cloud-dispatch.sh verbs

`bash scripts/gsd/cloud-dispatch.sh <verb> ...` is a thin wrapper that runs
`cloud_dispatch.py`. It looks for the library in `$FFS_LIB_DIR`, then
`<script>/../../lib`, then `~/.claude/lib/feature-fix-swarm`. Every verb
takes `--root ABS`, the absolute path of the main worktree (default: from
the current directory; a linked worktree is refused).

| Verb | Arguments | Effect |
|---|---|---|
| `plan` | `--tasks FILE --policy auto\|prefer\|required [--run R] [--with-doctor] [--sequential]` | Prints the route plan as one JSON document. Writes nothing. Refuses a task id the secret scanner flags (`SECRET_IN_PROMPT`, exit 3) before it prints the plan, as `start` does. Reads config from the working tree. With `--run` it also reports which grants are missing. `doctor` runs only with `--with-doctor`; otherwise `preconditions.doctor` is `unknown`. |
| `start` | `--run R --tasks FILE --policy auto\|prefer\|required [--foreground] [--sequential]` | Runs the run-level refusals, `doctor`, the router, and the grant checks. Scans task text and packets. Writes the ledger and `tasks.json`, creates the integration ref (an existing one is never adopted: `INTEGRATION_REF`, and `INTEGRATION_REF_UNCONFIRMED` when an identical retry of a crashed start finds it, exit 78; delete the ref by hand and start again), and spawns the orchestrator. Prints `CLOUD:started run=R tasks=N`, `CLOUD:local tasks=<ids>` (always printed, empty when no task is local; `/swarm` runs locally only these ids, never the plan's groups, because `start` routes again) and `CLOUD:orchestrator-started pid=N`. Refuses a run id that already has a ledger. |
| `orchestrate` | `--run R [--resume] [--detach] [--once] [--wait-lock SECONDS]` | Runs the orchestrator. `--detach` re-spawns it in its own session and prints `CLOUD:detached pid=N`. `--once` runs a single tick. `--wait-lock` waits up to that many seconds for the orchestrator lock instead of exiting at once, and exits 75 if the lock stays held. `--resume` changes nothing in the orchestrator itself; startup always resumes a parked run. |
| `status` | `--run R [--json]` | Read-only. Per task: state, provider, generation, branch, URL, blocker, decisions, review and verify evidence. Also orchestrator pid, liveness, heartbeat age, and the integration tip. |
| `doctor` | `[--json]` | Runs the checks in [Doctor checks](#doctor-checks). |
| `env init` | `[--register-hook] [--force]` | Writes the four `.ffs/cloud/` files from the templates. Never commits. |
| `env-spec` | `--provider claude\|codex` | Prints the one-time provider steps. |
| `cleanup` | `--run R [--remote] [--discard-unmerged]` | Only for `done` runs, and for a run that is not `done` and has no task inputs file and no task that ever started (a `start` that crashed early). Holds the run ledger lock for its whole duration: a concurrent operator verb waits, then reports `LEDGER_MISSING`. Deletes the local refs the run ledger accounts for (the integration ref, the task branches, the candidate and publication refs), each only while it still points at the OID the ledger recorded, plus run state and slot entries. Any other ref under `ffs/cloud/<run>/` is kept and printed as `CLOUD:cleanup-kept ref=<ref> (<reason>)`. A ref the ledger accounts for but recorded no OID for is kept the same way, because it may hold anything now. A task branch (local or pushed) counts as the dispatcher's only when the ledger holds a record written after the run saw it absent and created it (the admit journal entry's `created`, written with the confirmed push), and the integration ref only when the initialization record says `integration_ref_created`. The admit intent records the base OID before anything is scanned or pushed and is not proof. A ref without that proof (a branch that existed before the run pushed, a task abandoned after a scan refusal, a ref of a start that crashed) is kept and printed `CLOUD:cleanup-kept ref=<ref> (creation not confirmed)`, and does not fail `cleanup`. A ref name the secret scanner flags prints as `[withheld sha256:<12 hex>]`, not as the name. A ref that moved off its recorded OID is left, the ledger is kept and `CLEANUP_INCOMPLETE` names it (put it back or delete it by hand, then rerun). Nothing is deleted until every local ref is classified, and each delete compares the OID it saw. `--remote` also deletes the pushed task branches (`<task>-g<N>`) and needs the push grant. Each remote delete uses a lease on the recorded OID, so a branch that moved is kept, printed as `CLOUD:cleanup-kept` and named in `CLEANUP_INCOMPLETE`; a remote task branch with no recorded OID or no proof the run created it is kept. A remote `ffs/cloud/<run>/integration` branch is never deleted: the integration ref is published only locally, so it is not the dispatcher's and is reported as left behind. Refuses with `INTEGRATION_UNMERGED` while the integration ref holds commits that no other branch contains, unless `--discard-unmerged`. It also refuses with `INTEGRATION_UNMERGED` when git fails while reading the integration ref (a ref that is simply absent is fine), because an unreadable ref cannot be shown to be merged. A remote-tracking ref (`refs/remotes/*`) is a local cache and counts as a branch that contains them only when `ls-remote` shows the same branch at the same OID on `origin`. If any ref deletion fails, a ref listing fails, or a remote ref under the run prefix is not a name the dispatcher creates, `cleanup` keeps the run ledger, prints `CLOUD:cleanup-partial` and `CLOUD-REFUSED: CLEANUP_INCOMPLETE` naming the refs left behind and exits 3; fix the cause (delete a foreign remote ref by hand) and rerun. A failed remote delete stops before any local ref is deleted. `--remote` deletes only `ffs/cloud/<run>/*`; it never deletes the provider-created `claude/ffs-*` result branch, so delete that by hand (`git push origin --delete <branch>`). A run whose task never bound stays `active`, and `cleanup` refuses it (`RUN_NOT_DONE`) until you `abandon` the task. |
| `probe-claude` | `--dir DIR [--seconds N]` | Operator check. Starts the plain `claude` binary (no `--cloud`, no prompt) on a pseudo-terminal in `DIR`, prints the first screen as JSON (`dialog`, `markers`, `screen`) and hangs up. `--seconds` is clamped to 1 to 60 (`nan` falls back to the default, so the probe always has a deadline). Sends no keystroke and creates no cloud session. Use it to see whether a trust or login dialog appears before a real launch. |
| `adopt` | `--run R --task T (--oid OID \| --remote-id ID)` | Operator-only. Pass exactly one of the two. `--oid` binds a Claude result commit to an `uncertain` task or one blocked by `uncertain-launch`, `poll-errors`, `ambiguous-candidates` or `candidate-moved`. The task re-enters at `result-ready`. `--oid` is refused for Codex tasks, which have no OID before FFS applies the diff. `--remote-id` binds the Codex task id you found in the provider UI to a Codex task in the same states. The task goes back to `launched` with a new deadline of `deadline_hours` from now, and the normal poll and fetch follow. `--remote-id` is refused for Claude tasks. |
| `abandon` | `--run R --task T [--retry]` | Operator-only. Bumps the generation and issues a new nonce. The task becomes `abandoned`, or `waiting` with `--retry`, which also works from `rejected` and `failed` and reopens dependents that failed only because of it. With `--retry` on a `rejected` or `failed` task of a `done` run, the run becomes `active` again. Refused with `INTEGRATION_IN_PROGRESS` (exit 3) while the task is `integrating`, because the integration ref may already have moved. Retry after the orchestrator's next tick. It does not stop the provider session. |
| `resolve` | `--run R --task T --action retest\|accept-deps\|reject` | Operator-only. Clears a `needs-operator` blocker. Refused unless the task's blocker belongs to the action. `accept-deps` is an action of `resolve`, not a separate verb. See [Blockers](#blockers) and [Runbook: dependency change](#runbook-dependency-change). |

### Exit codes and output

| Code | Meaning |
|---|---|
| 0 | Success. For `orchestrate` also: run done, run parked, or another orchestrator already holds the lock. |
| 2 | Usage or input error (bad arguments, bad run or task id, invalid tasks file or annotation). |
| 3 | Refused. The line `CLOUD-REFUSED: <CODE> <detail>` is printed on stdout. `doctor` uses 3 for not green. `RUN_EXISTS` is the exception to "run it locally": `start` found a ledger for that run id (typically a retry after a lost acknowledgement), so the tasks may already be running in the cloud. The refusal ends with `batch=same` (optionally `local-claimed=<ids>`), `batch=other` or `batch=unknown`, and may then add `orchestrator=restarted` or `orchestrator=restart-failed` (the identical retry found an active run with no orchestrator and the spawn failed with an `OSError`: nothing else changed, the local claims stand, and the same retry tries again once the cause is fixed). The batch token comes from comparing the retried batch (every task id and its content, its `depends_on` and its order, cloud and local) with the whole batch the first attempt started, which the ledger stores in `batch`. `batch=same` needs an exact match, so a superset, an edited batch, a changed order or a changed dependency (also from `--sequential`) is `batch=other`; a ledger without the stored batch or claimed-task list (an older release) gives `batch=unknown`. `local-claimed=<ids>` names the local tasks the first attempt claimed before it printed them; a retry never offers them as `local=` again, because that caller may already have started them. `orchestrator=restarted` means an identical retry found an active run that no orchestrator owned and started one. `start` takes the run's orchestrator lock while it reserves the run, creates the integration ref and commits the initialization record, so a start that loses a race gets `RUN_EXISTS` too, and an identical retry of a start that died before that record finishes it. `/swarm` reads `status --run <run>`, never starts the run again and never runs a task the ledger holds; on `batch=same` it runs `reconcile.sh` once if the orchestrator is dead and the line did not say `orchestrator=restarted`; it starts a claimed local task again only after the operator confirms, and runs none of the local groups that contain one until the operator has said which to run; on any other answer it stops and never starts those cloud tasks under a new run id. `RUN_NOT_INITIALIZED` is also exit 3: `orchestrate` (a resume included) found a run with no initialization record (this includes a run created by an earlier build). An identical retry of `start` finishes a run that this build's `start` left uninitialized; a run from an earlier build has no stored batch record, so its retry ends `batch=unknown` and finishes nothing. `SECRET_IN_PROMPT` is exit 3 too: `start` scans every task id, local ones included, and the task text, and names only the scanner class, never the value. A route error that quotes a task id or dependency name the scanner flags prints the scanner class instead of the name. |
| 4 | `status` only: ok, but at least one task is `needs-operator` or `uncertain`. |
| 75 | `orchestrate --wait-lock` gave up because the orchestrator lock stayed held. Nothing else returns 75. |
| 78 | Store, config or internal error. The line `CLOUD-ERROR: <CODE> <detail>` is printed on stderr. |

Human lines start with `CLOUD:`. `plan`, `status --json` and `doctor --json`
print one JSON document.

`status` prints lines like these (a missing URL prints as `None`):

```text
CLOUD:status run=<run> state=active epoch=1 orchestrator-pid=<pid> alive=true heartbeat-age-s=14 tip=<oid>
CLOUD:task t1 state=running provider=claude generation=1 branch=refs/heads/ffs/cloud/<run>/t1-g1 url=None blocker=None
CLOUD:decision t1 <text the worker or a gate left for you>
CLOUD:integration ref=refs/heads/ffs/cloud/<run>/integration
```

### Doctor checks

Each check prints `CLOUD:doctor <LEVEL> <CODE> <detail>`, and the last line
is `CLOUD:doctor green` or `CLOUD:doctor not-green`. Green means no `FAIL`.

| Code | Level | Check |
|---|---|---|
| `PROVIDER_CLAUDE`, `PROVIDER_CODEX` | OK or WARN | Binary found and no API-key variable set. Codex also needs its environment id variable and a limit above 0. |
| `LOGIN_CLAUDE`, `LOGIN_CODEX` / `LOGIN_UNVERIFIED` | OK or WARN | Best-effort login probe, run for each usable provider: `claude auth status`, `codex login status`. `LOGIN_UNVERIFIED` means the CLI has no such subcommand, the probe failed or it timed out (15 seconds). It is never a `FAIL`. |
| `API_KEY_BACKEND` | FAIL | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set. |
| `NO_PROVIDER` | FAIL | No provider is usable. |
| `ORIGIN_HAS_CREDENTIALS` | FAIL | The `origin` URL embeds credentials. `doctor` reports only this check and stops, before any command receives the URL. |
| `ORIGIN_GITHUB` | FAIL | `origin` is not a GitHub remote. |
| `LIFECYCLE` / `LIFECYCLE_OFF` | OK or FAIL | `FFS_LIFECYCLE=off` fails. |
| `RECONCILE_OFF` | WARN | `FFS_RECONCILE=off`. |
| `SANDBOX` | FAIL | `sandbox-exec` is missing, or its startup self-check fails. See [Sandboxed tests](#sandboxed-tests). |
| `REVIEW_SCRIPTS` / `REVIEW_UNAVAILABLE` | OK or FAIL | `cloud-review.sh` and `adversary-host.sh` are found, or at least one is missing. See [Review by the opposite model family](#review-by-the-opposite-model-family). |
| `BASE`, `BASE_CLEAN`, `BASE_PUSHED` | FAIL | No commit at HEAD; tracked files modified; HEAD not contained in any remote branch. |
| `CLOUD_FILES` | FAIL | A file of `.ffs/cloud/` is missing from the base commit. |
| `SESSION_HOOK` | WARN | No `SessionStart` entry for `session-start.sh` in the committed `.claude/settings.json`. |
| `VERIFY_CMD` | WARN | `config.verify.cmd` is empty. |
| `RECONCILE_SCRIPT`, `RECONCILE_MAPFILE` | OK or WARN | `reconcile.sh` missing, or it uses `mapfile`. |
| `RECONCILE_AGENT` | WARN | The reconcile scheduler is not installed. |
| `SECRETS_DECLARED` | FAIL | Only when tasks are given (`plan --with-doctor`, `start`): a `needs-secret` name is not in any `exposure` list. |
| `RULESET` | FAIL or WARN | No active ruleset covers `ffs/cloud/**`. FAIL when any secret name is exposed, WARN otherwise. |
| `RULESET_BYPASS` | FAIL | A covering ruleset has a bypass actor other than the repository admin role, or the list cannot be read. |
| `HISTORY_SECRET` | FAIL | Only when Claude is usable: secret-shaped content, binary blobs or secret-named files in the base history. The line shows a count and the first 20 findings; `doctor --json` lists all of them. See [Doctor and history findings](#doctor-and-history-findings). |

### Config file

`.ffs/cloud/config.json`, committed, names only. A run reads it from the
commit it started on. Missing keys take the defaults below.

| Key | Default | Meaning |
|---|---|---|
| `schema` | `1` | Reserved. Not checked. |
| `limits.claude`, `limits.codex` | `3`, `3` | Concurrent sessions per provider. `0` disables the provider. |
| `limits.total` | `6` | Concurrent sessions across all runs. |
| `limits.max_local_verifiers` | `1` | Results collected, reviewed and tested at once, across all runs. |
| `verify.cmd` | `""` | Default test command. Empty makes every task without `[verify]` ineligible. |
| `verify.timeout_s` | `900` | Wall limit for one test run. |
| `verify.deps` | `["node_modules", ".venv"]` | Directories of your main worktree linked read-only into the test tree, if they exist. |
| `verify.read_roots` | `[]` | Extra absolute directories the sandbox may read. Not in the template. |
| `secrets.provider` | `"none"` | `none`, `provider-env`, `doppler` or `command`. See [Secrets](#secrets). Other values are not rejected and behave like a non-`none` mode. |
| `secrets.wrapper` | `""` | Command prefix that workers use for test and runtime commands. |
| `secrets.install` | `""` | Install command for the secrets CLI (`command` mode). |
| `secrets.exposure.claude`, `.codex` | `[]` | Every secret name reachable from that provider's environment. Not only the ones tasks use. |
| `secrets.network_secret_hosts` | `[]` | Present in the template for documentation. No code reads it. |
| `scan.allow` | `[]` | Allowed history findings: `{"blob": "<oid>", "class": "<class>"}` for findings that carry a blob id, or `{"path": "<repo path>", "class": "<class>"}` for a `secret-filename` finding. Read from the base commit, so commit it. |
| `codex.env_id_var` | `"FFS_CODEX_CLOUD_ENV"` | Name of the environment variable that holds the Codex environment id. |
| `deadline_hours` | `12` | Whole hours a launched session may run. Also bounds the worker's time budget. |

Secret names should be `UPPER_SNAKE` starting with a letter, 64 characters
or fewer. The manifest validator rejects other names in `env_report`.

### Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `FFS_CLOUD` | `/swarm` skill | Turns the cloud block on without the flag. |
| `FFS_CODEX_CLOUD_ENV` | dispatcher | Codex environment id. The variable name is `config.codex.env_id_var`. Without it Codex is not usable. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | `doctor`, launch | Must be unset. If set, the provider is refused (`API_KEY_BACKEND`). |
| `FFS_LIFECYCLE` | `doctor`, `start` | `off` refuses cloud runs. |
| `FFS_RECONCILE` | `doctor` | `off` disables reconcile restarts (WARN). |
| `FFS_MANAGED_INGRESS` | router | Any value except empty or `0` keeps the run local. |
| `GSD_TOKEN_BUDGET` | router | Any non-empty value keeps the run local. |
| `DIGEST_NOTIFY_CMD` | orchestrator | Command that receives the `CLOUD-PARKED` line on stdin. |
| `FFS_LIB_DIR` | `cloud-dispatch.sh` | Library directory override. |
| `FFS_CLOUD_LAUNCH_TIMEOUT_S` | backends | Launch timeout in seconds, 180 at most. |
| `FFS_CLOUD_CLONE_PARENT` | Claude backend | Unset (default): the launch clone is a fresh directory under the system temp dir, which always shows the client's trust dialog. Set: the clone goes to the fixed path `<parent>/ffs-cloud-launch-claude`. Claude Code trusts a folder per git root, so clone the repository to that exact path, run the interactive client once in that directory and accept the trust dialog yourself; FFS never answers it (see [Quick start](#quick-start), step 5). The parent must be an absolute path to an existing directory that is not a symlink, is owned by you, and is not inside the repository. Anything else fails the launch with `CLONE_PARENT_INVALID <reason>` (there is no fallback to the temp dir). Use of the fixed path is serialized with an `flock` on `<parent>/.ffs-cloud-launch-claude.lock` held for the whole launch (a second launch waits, then fails with `CLONE_LOCK_BUSY`). The dispatcher creates the clone directory in a private staging directory inside the parent (`.ffs-cloud-new-*` / `.ffs-cloud-old-*`, mode 0700), writes an ownership marker beside the final path (`<parent>/ffs-cloud-launch-claude.ffs-owned`, naming the directory's device and inode, written atomically and without replacing anything) and publishes the directory with a rename that fails if the path exists. A stale clone at that path is removed before cloning only if the marker names it, verified after the clone is renamed into staging, and the clone is removed after every launch, including one that raises or is killed at the transfer timeout; a launch killed part-way is recovered by the next launch (its own empty staging directories, an empty own marker and its own marker temporary files only; a non-empty `.ffs-cloud-new-*` directory is kept and needs manual removal). Any other existing path (a directory without the marker, a symlink, a plain file) is refused (`CLONE_PATH_INVALID`, with a reason such as `MARKER_UNRECOGNIZED` or `MOVED_AWAY` where one applies) and never deleted; a file at the marker path that is not a dispatcher marker is never removed or truncated. Remove an operator-made clone once by hand. |
| `FFS_CLOUD_PTY_GRACE_S` | Claude backend | Seconds to keep reading after the session URL appears, before the terminal is hung up. 0 to 120, default 20. |
| `FFS_CLOUD_BIN_CLAUDE`, `FFS_CLOUD_BIN_CODEX` | backends | Path to a provider binary instead of `PATH`. Used by tests. |
| `FFS_CLOUD_ALLOW_NON_GITHUB_ORIGIN` | `doctor` | `1` downgrades the GitHub check to WARN. A test seam; real providers clone only from GitHub. |
| `CLAUDE_CODE_REMOTE`, `FFS_CLOUD_PROVISION`, `FFS_CLOUD_SETUP_TIMEOUT` | `session-start.sh` | See [Claude environment](#claude-environment). |

Provider CLIs and `git` run with an allowlisted environment: `PATH`, `HOME`,
`LANG`, `LC_ALL`, `TERM`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
`XDG_CONFIG_HOME` and `SSH_AUTH_SOCK`. Nothing else is inherited. The
dispatcher's local `git` helpers (quarantine `init`/`fetch`/`checkout`, ref
updates, the scanner and collector) inherit the rest of your environment
except the variables that select a repository or change how git behaves
(`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`,
`GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`, `GIT_CONFIG`,
`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`, `GIT_CONFIG_VALUE_*`,
`GIT_CONFIG_PARAMETERS`, `GIT_EXEC_PATH`, `GIT_SSH_COMMAND`,
`GIT_EXTERNAL_DIFF`, `GIT_TEMPLATE_DIR` and similar), which are removed.
`GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` are kept, as `HOME` is. Every
dispatcher git process runs with `GIT_NO_REPLACE_OBJECTS=1`, so a
`refs/replace/*` ref cannot change what the scanner or collector reads. A
`GIT_DIR` exported in your shell cannot redirect a cloud run into your
checkout. Every dispatcher push passes `--no-follow-tags` and
`--recurse-submodules=no`, so a local annotated tag or unpublished submodule
work is never published, whatever `push.followTags`, `push.recurseSubmodules`
or `submodule.recurse` say.

### State directory

Under the main worktree, gitignored: `.feature-fix-swarm/cloud/`.

```text
providers.json, providers.lock     slots ledger shared by all runs
scan-cache.json                    history scan results keyed by commit and allowlist signature
verify.<i>.lock                    one per local verifier slot
runs/<run>.json, <run>.lock        run ledger
runs/<run>.orch.lock               orchestrator lock, holds the pid
runs/<run>/tasks.json              cloud tasks with prompts, mode 0600
runs/<run>/orchestrate.log
runs/<run>/launch-<task>-g<N>.log
runs/<run>/collect-<task>-g<N>.log
runs/<run>/verify-<task>-g<N>.log  retest-<task>-g<N>.log after a merge
runs/<run>/manifest-<task>-g<N>.json
runs/<run>/dependency-<task>-g<N>.json
runs/<run>/claude_refs-<task>-g<N>.json
runs/<run>/quarantine/<task>-g<N>/
```

Logs are mode 0600 and redacted. Only `.ffs/cloud/*` and
`.claude/settings.json` are committed.

Git refs. Run ids match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$`.

| Ref | Meaning |
|---|---|
| `refs/heads/ffs/cloud/<run>/<task>-g<N>` | Task branch, pushed to origin, never moved after creation. |
| `refs/heads/ffs/cloud/<run>/integration` | Local integration branch. |
| `refs/ffs/cloud/<run>/cand/<task>-g<N>` | Frozen candidate commit (local). |
| `refs/ffs/cloud/<run>/pub/<task>-g<N>` | Pinned publish commit (local). |
| `claude/ffs-<run>-<task>-<nonce8>` | Result branch requested from the Claude worker. |

### Task states

Every cloud task starts `waiting`. Terminal states are `integrated`,
`rejected`, `failed`, `abandoned` and `superseded`. A dependent of a task
that ends terminal but not `integrated` becomes `failed` with blocker
`dependency-<state>`.

| State | Meaning | Holds a slot |
|---|---|---|
| `waiting` | Waiting for dependencies. | No |
| `admitted` | Dependencies integrated, branch pushed; waiting for capacity. | No |
| `reserved` | Slot taken; grants and branch checked before submit. | Yes |
| `submitting` | Launch in progress. The ledger is written before the provider command runs. | Yes |
| `launched` | The provider accepted the task. For Claude this can also mean the client was still running with no session URL at the launch timeout (`launch_note: "no-session-url"`), or had already exited with no session id or URL and no known error (`launch_note: "exit-no-session-url"`); polling by nonce continues. | Yes |
| `running` | Poll says running. | Yes |
| `uncertain` | The launch outcome is unknown. Never relaunched automatically. | Yes |
| `result-ready` | A result OID is bound. | No |
| `collected` | Candidate commit frozen. | No |
| `reviewed` | Opposite family approved. | No |
| `verified` | Sandboxed tests passed. | No |
| `integrating` | Merge planned and journaled. | No |
| `integrated` | On the integration ref. Terminal. | No |
| `needs-operator` | A blocker waits for you. Not terminal. A slot held before the blocker stays held until the task's launch deadline. The orchestrator then releases it, once. | Maybe |
| `rejected` | A gate refused the result. Terminal. | No |
| `failed` | Launch failed, deadline passed, or a dependency ended badly. Terminal. | No |
| `abandoned` | You abandoned it. Terminal. | No |
| `superseded` | Defined; nothing enters it in this release. Terminal. | No |

### Blockers

| Blocker | State | Set when | Clears with |
|---|---|---|---|
| `uncertain-launch` | `needs-operator` | An `uncertain` task found no session after its recovery polls: 3 for Claude, 1 for Codex. | `adopt --oid` (Claude) or `adopt --remote-id` (Codex), or `abandon [--retry]` |
| `poll-errors` | `needs-operator` | More than 6 consecutive failed polls. For Codex a provider `failed` status counts as a failed poll. | `adopt --oid` (Claude) or `adopt --remote-id` (Codex), or `abandon [--retry]` |
| `ambiguous-candidates` | `needs-operator` | Several Claude refs qualify and none carries the nonce, or a new `claude/*` ref name matched the secret scanner and the task's own result is not among the candidates (the decision text says so; the name is never shown). | `adopt --oid`, or `abandon [--retry]` |
| `candidate-moved` | `needs-operator` | Defined; the dispatcher never sets it in this release. | `adopt`, or `abandon [--retry]` |
| `manifest-blocked` | `needs-operator` | The worker wrote `status: blocked`. Its blockers and decisions are listed. | `abandon [--retry]` |
| `path-outside-owns` | `needs-operator` | A changed path does not match `owns`. | `resolve --action reject`, or `abandon --retry` |
| `denylist` | `needs-operator` | A changed path matches the denylist, holds a Unicode format or control character (Cf or Cc), or collides in case with another path (detail `case-colliding paths`). | `resolve --action reject`, or `abandon --retry` |
| `dependency-change` | `needs-operator` | A dependency or lock file changed. | `resolve --action accept-deps` (needs the `install:<pkg>` grants, and `install:unattributed:<digest12>` when the change cannot be attributed to named packages), or `reject` |
| `review-unavailable` | `needs-operator` | Review failed to produce a verdict, the diff was too large, the review script is missing, or the outbound scan of the review text refused. | `resolve --action retest` (refused after two retests of this task generation) or `reject` |
| `review-model-downgraded` | `needs-operator` | The review approved, but a weaker model than the requested judgment-tier model answered. The detail names both models. | `resolve --action retest` (counts against the same two-retest limit as the other review blockers, counted from the stale re-review when one began) or `reject` |
| `review-model-unknown` | `needs-operator` | The review approved, but the answering model could not be determined. | `resolve --action retest` (same limit) or `reject` |
| `verify-cmd-missing` | `needs-operator` | The effective verify command was empty or whitespace-only when verification ran. The router refuses such a task, so this is a defensive stop for a hand-edited or older ledger. No tests ran. | `resolve --action reject` or `abandon --retry` |
| `manifest-invalid` | `needs-operator` | The task's own result branch (or a manifest carrying its nonce) has a manifest the validator rejected. The detail names the validator error code. | `abandon [--retry]` |
| `binary-change` | `needs-operator` | The result changes binary content. This always blocks; there is no allowlist. The reviewer cannot read the bytes. | `resolve --action reject` or `abandon --retry` |
| `review-cap` | `needs-operator` | The two review rounds of the current meter are spent. | `resolve --action retest` (fresh meter; refused after two retests of this task generation, counted from the stale re-review when one began), `resolve --action reject` or `abandon --retry` |
| `sandbox-unavailable` | `needs-operator` | The sandbox cannot run, or the tree cannot be materialized. | `resolve --action retest` after the fix, or `reject` |
| `merge-conflict` | `needs-operator` | The tip and the candidate changed overlapping paths (FFS merges only disjoint paths), the merged tree was not the exact expected tree, the integration ref moved outside FFS, the integration ref already holds the published result but the task's approval has no reviewer-model evidence (reset the ref to the ledger tip first), a case collision between two results in the merged tree (detail `case-colliding paths`), a git error while integrating, or the integration ref is a symbolic ref (FFS never moves it). | `resolve --action retest` after the fix, or `reject` |
| `patch-id-mismatch` | `needs-operator` | The merged change differs from the reviewed change. | `resolve --action retest`, or `reject` |
| `exposure-not-granted` | `needs-operator` | A `secret-use:<NAME>` grant is missing at submit. | Grant it, then `resolve --action retest` |
| `ruleset-unverified` | `needs-operator` | Active `update` and `deletion` rules for the task branch could not be confirmed (secrets exposed). | Fix the ruleset, then `resolve --action retest` |
| `grant-missing` | `needs-operator` | A push or `cloud-dispatch` grant is missing. | Grant it, then `resolve --action retest` |
| `scan-refused` | `needs-operator` | The outbound scan refused a push of a task branch, or the packet scan refused a submit. The detail lists finding classes and locations. | Fix the finding, then `resolve --action retest` |
| `branch-conflict` | `needs-operator` | The task branch exists locally or on origin at a different OID, or at submit it is no longer at the admitted OID on origin (moved or deleted). | Fix the branch, then `resolve --action retest` |
| `push-failed` | `needs-operator` | The push of a task branch failed 5 times, for example a protected-branch rejection. | Fix the cause, then `resolve --action retest` |
| `internal-error` | `needs-operator` | One step of one task failed 3 times in a row, or a collect blocker this version does not know or cannot apply from the task's current state. The detail names the error type. Other tasks keep running. | `resolve --action retest` |
| `integration-ref-foreign` | `needs-operator` | After a restart, the integration ref holds commits on top of the published result that FFS did not publish. | Review the commits, then `resolve --action reject` or `abandon` |
| `secret-leak` | `rejected` | The scan found secret-shaped content, at collect or in the review diff or prompt. `status` prints the runbook. | None. `abandon --retry` starts a new generation. |
| `review-revise` | `rejected` | The reviewer said `REVISE`. | None. `abandon --retry`. |
| `tests-failed` | `rejected` | Sandboxed tests failed, on the result or on the merged tree. | None. `abandon --retry`. |
| `quarantine-refused` | `rejected` | Fetch, ancestry, symlink, submodule, size, commit-count, manifest or candidate checks failed, or the scanner itself failed during collect. The detail names the reason. | None. `abandon --retry`. |
| `deadline` | `failed` | `deadline_hours` passed since launch. | `abandon --retry` |
| `dependency-<state>` | `failed` | A dependency ended terminal and not `integrated`. | `abandon --retry` on the dependency reopens it |

`resolve --action retest` re-runs the check that stopped the task: review,
tests, integration, submit, or admit. `accept-deps` needs the file
`dependency-<task>-g<N>.json` in the run directory, which collect writes. Its
digest must match the one recorded for the bound result. If the generation, the
bound result or the report changed after the grants were checked, `resolve`
refuses with `STALE_APPROVAL`, and you run it again.

### Grants

Grants live in the FFS grant ledger (`lib/gates.py`). `check-grant` exit 0
means granted, 1 not granted, anything else is treated as a refusal.

| Key | Authorizes | Checked |
|---|---|---|
| `cloud-dispatch:claude`, `cloud-dispatch:codex` | Submitting tasks to that provider. | At `start` and before every submit. |
| `push:origin/ffs/cloud/<run>` | Non-force pushes of `refs/heads/ffs/cloud/<run>/integration` and `<task>-g<N>`, and nothing else. Also `cleanup --remote`. | At `start`, before every push and before `cleanup --remote`. |
| `secret-use:<NAME>` | Exposing `NAME` to the cloud. One per name in `exposure` of every provider used, plus each `needs-secret` name. | At `start` and before every submit. |
| `install:<pkg>` | `resolve --action accept-deps` for each added or changed package. | At `resolve`. |
| `install:unattributed:<digest12>` | `resolve --action accept-deps` when the change cannot be attributed to named registry packages. `<digest12>` is the first 12 characters of the report digest, which the decision text prints. It binds the grant to this one report. | At `resolve`. |

Record grants with the FFS grant ledger:

```bash
python3 lib/gates.py grant <run> --action cloud-dispatch:claude \
  --action push:origin/ffs/cloud/<run> --ttl-hours 24 --reason "cloud run"
```

Grants expire. An expired grant stops the next admit or submit with
`grant-missing` or `exposure-not-granted`. Grant again and run
`resolve --action retest`.

### Denylist

Glob entries with a `/` are anchored at the repository root. Entries without
a `/` match a path component at any depth. `plan` refuses `owns` globs that
could name these, and collect rejects results that touch them.

```text
.ffs/**  .ffs-result/**  .planning/**  .claude/**  .codex/**  .github/**
.feature-fix-swarm/**  .git*  .gitmodules  scripts/hooks/**  hooks/**
.husky/**  .circleci/**  .gitlab-ci.yml  .mcp.json  CLAUDE.md  AGENTS.md
.envrc  .npmrc  .env*  *.pem  id_rsa*  setup.sh
```

Note that `.git*` also covers `.gitignore` and `.gitkeep`, and that
`setup.sh` and `.env*` match at any depth.

### Guard patterns

`scripts/hooks/operator-only-guard.sh` is a PreToolUse hook for agent shell
calls. It now also denies:

- `claude` with `--cloud`.
- `codex cloud` followed by `exec`, `apply` or `diff`.
- `cloud-dispatch.sh` (or `cloud_dispatch.py`) with `adopt`, `abandon` or
  `resolve`.

The three cloud patterns match by executable position. The guard looks at the
first command of each shell segment, after leading `VAR=value` assignments,
wrappers such as `env`, `command`, `exec`, `sudo`, `nohup`, `time` and `rtk`,
and shell or Python launchers (`bash -c`, `bash SCRIPT`, `python3 SCRIPT`).
It compares the command by base name, so `/opt/bin/claude --cloud` is denied
and `git grep -e codex -e cloud -e diff` is allowed. The older operator-only
patterns still match by token membership.

The `/swarm` session itself never calls the provider CLIs; the orchestrator
does, as a subprocess the guard does not see. The guard fails open, and
indirection through script files or imports bypasses it, so it is an
anti-accident control, not a security boundary.

### Installer

The installer copies `cloud_dispatch.py`, `cloud_ledger.py`, `cloud_host.py`,
`cloud_scan.py`, `cloud_sandbox.py`, `cloud_collect.py`,
`delegation_router.py`, `handoff_packet.py`, `operator_guard_hook.py`,
`cloud-dispatch.sh` and `cloud-review.sh`, and ships `templates/cloud/*` as
`cloud-templates/*` next to `cloud_dispatch.py`. `env init` copies from
there.

It also ships the closure that `cloud-review.sh` needs to the managed lib
(`~/.claude/lib/feature-fix-swarm`): `scripts/gsd/adversary-host.sh`,
`scripts/gsd/run-bounded.sh`, `scripts/gsd/fence-data.sh`, `lib/gates.py` and
`lib/model_requests.py`. The managed lib keeps `gates.py` and
`model_requests.py` flat for other tools, so they are shipped a second time
under `lib/`.

## Cloud environment setup

FFS cannot create, select or verify a provider environment from the CLI.
`env-spec` prints the steps. Use a private environment for each provider.
List every secret name reachable from it in `secrets.exposure.<provider>`.

### Claude environment

1. Create or pick a private environment for the repository in the Claude
   Code web settings.
2. Setup script: leave it empty. The committed hook runs
   `.ffs/cloud/setup.sh` itself.
3. Network: choose a level and allowed domains that cover the package hosts
   your lockfile needs. With the `doppler` mode also allow `api.doppler.com`
   and the host that serves the Doppler CLI download. The level names and the
   domain list belong to the provider and are not read by FFS. The hosts a
   Doppler-enabled setup needs are given per the provider's and Doppler's
   documentation at the time of writing. Confirm them in your own account
   before you rely on them.
4. Secrets: see [Secrets](#secrets).

`session-start.sh` is the `SessionStart` hook registered by
`env init --register-hook` as
`bash "$CLAUDE_PROJECT_DIR/.ffs/cloud/session-start.sh"` with a 300 second
hook timeout. It acts only when both hold:

- `CLAUDE_CODE_REMOTE=true`, and
- the checked-out branch matches `ffs/cloud/*`, or `FFS_CLOUD_PROVISION=1`.

Otherwise it exits 0 without output, so your ordinary sessions are
untouched. When it acts it runs `setup.sh` with a cap of 240 seconds
(`FFS_CLOUD_SETUP_TIMEOUT` overrides it), always exits 0, and writes
`~/.ffs-cloud-setup.log` and `~/.ffs-cloud-env-status` in the session. Any
ordinary session you start by hand on an `ffs/cloud/*` branch is provisioned
too.

`setup.sh` is idempotent. It re-runs itself under `env -i`, so installers
start with a clean environment instead of the session's variables (see
[Secret isolation on Linux](#secret-isolation-on-linux)). It then installs
repository dependencies from lockfiles
(`npm ci`, `pnpm install --frozen-lockfile`, `yarn install
--frozen-lockfile`, `uv sync --frozen`, `pip install -r requirements*.txt`),
`jq`, `bats` and `shellcheck` through `apt-get` when they are missing and it
may use it, and the secrets CLI for `doppler` or `command` mode. It writes no
secret to disk. A fully successful run leaves a stamp
`~/.ffs-cloud-setup.<sha256 of setup.sh>` and later runs exit at once. A
failed step leaves no stamp, so the next session retries. Whether the
provider keeps that home directory between sessions is undocumented by the
provider, and FFS does not rely on it.

The worker is told to run `bash .ffs/cloud/env-check.sh <provider>` first,
with `claude` or `codex` appended. The script exits 2 without a provider. It
prints exactly one JSON object, the shape of the manifest's `env_report`:
tool versions in `tools`, and which names from `secrets.exposure.<provider>`
are `present` or `missing`. Names only. `env-check.sh <provider> --status`
prints the setup status file instead (or `null`).

#### Secret isolation on Linux

The `env -i` re-exec gives installers a clean environment, not a hidden
process tree. On Linux an installer that runs here, for example an npm or pip
lifecycle script, can read `/proc/<pid>/environ` of every ancestor process
with the same user id. One of those is the Claude Code process that started
the hook, and it still holds the session's tokens. FFS cannot unset a
parent's environment before the re-exec, and `setsid` or a double fork does
not change that. So `env -i` is the only isolation, and the `/proc` exposure
is a documented residual risk. The status file `~/.ffs-cloud-env-status`
records it as `"secret_isolation":"env-i-only"`. Treat every dependency
installed in the cloud session as able to read the secrets the session
holds. The real boundary is the provider environment and the secrets you
expose to it.

### Codex environment

1. Create or pick a private environment for the repository in the Codex
   cloud settings.
2. Export its id where the dispatcher runs: `FFS_CODEX_CLOUD_ENV=<id>`
   (the variable name is `config.codex.env_id_var`).
3. Setup commands and the internet access setting of the environment are
   provider settings. What an environment's setup phase may do and which
   internet policy applies are undocumented here and not read by FFS. Allow
   the hosts your tests and dependency installs need.
4. Secrets: `env-spec` tells you to use environment variables for the task
   phase, because the Codex Secrets store is described as available during
   environment setup only. That is per the provider's documentation at the
   time of writing. Confirm it in your own Codex environment settings, since
   the provider can change it.

The Codex backend runs `codex cloud exec --env <id> --branch <task branch>
<prompt>`, `codex cloud status <id>` and `codex cloud diff <id>`. These
subcommands and their output shapes are unverified against the real CLI (the
Codex backend was not run live); the parsers accept JSON with `id` and `status` keys, or text forms, and report
`unknown` otherwise.

## Secrets

FFS never reads, stores or sends a secret value. Values live in the provider
environment, put there by you. FFS handles names only.

Two rules apply in every mode:

- Exposure is the whole environment. Every worker session sees everything
  the environment exposes. So `secrets.exposure.<provider>` must list every
  name reachable there, and each listed name needs a `secret-use:<NAME>`
  grant before any task is submitted to that provider, including tasks that
  use no secrets. A missing grant stops the run at `start` (autonomous) or
  needs your yes (interactive, which lists the names).
- Names in `exposure` can be `host:<name>` for Claude network secrets. They
  are granted as `secret-use:host:<name>` and are skipped by `env-check.sh`.

| Mode | What FFS does |
|---|---|
| `none` | Default. No secret handling. |
| `provider-env` | Workers read the values as environment variables of the session. No wrapper. |
| `doppler` | `setup.sh` installs the Doppler CLI. The worker prompt carries your `secrets.wrapper` if you set one. |
| `command` | `setup.sh` runs `secrets.install` through `bash -c`. The worker prompt carries your `secrets.wrapper`. |

### Mode none

The default. Leave `exposure` empty. Workers get no secrets. Mark any task
that needs one `[local]`, or use a mode below.

### Mode provider-env

1. Set `"provider": "provider-env"` and list every name in
   `exposure.claude` and/or `exposure.codex`.
2. Put the values in the provider environment as environment variables. For
   Claude HTTP API keys you can use the provider's network secrets instead
   and list them as `host:<name>`. For Codex use environment variables.
3. Use dedicated, non-production values.
4. Annotate tasks with `[needs-secret: NAME]` so routing checks coverage.

### Mode doppler

Doppler is one optional provider. Nothing else depends on it.

1. Set `"provider": "doppler"`.
2. Set `"wrapper": "doppler run --no-fallback --"`. The template default is
   empty, and with an empty wrapper the worker is told no wrapper is needed.
   Check the flags with `doppler run --help` for your CLI version.
3. In `.ffs/cloud/setup.sh` replace the three `REPLACE_AT_ENV_INIT` values
   (`DOPPLER_VERSION`, `DOPPLER_SHA256_LINUX_AMD64`,
   `DOPPLER_SHA256_LINUX_ARM64`) with a CLI version and the release archive
   checksums you verified. `env init` does not fill them. While any value is
   a placeholder the CLI is not installed, the step is recorded as
   `doppler: unpinned` and no stamp is written.
4. Create a dedicated config for agents, then a short-lived read-only token
   (placeholders in angle brackets):

   ```bash
   doppler configs tokens create ffs-cloud -p <project> -c <env>_agent \
     --max-age 720h --plain
   ```

   Paste it into the provider environment as `DOPPLER_TOKEN`.
5. List every name the token can read and put them all in `exposure`:

   ```bash
   doppler secrets --only-names -p <project> -c <env>_agent
   ```

6. Allow `api.doppler.com` and the CLI download host in the provider's
   network settings.

### Mode command

For any other secrets tool.

1. Set `"provider": "command"`.
2. Set `"wrapper"` to the prefix that injects secrets, for example
   `<your-cli> run --`. One short printable line, 200 characters at most.
3. Set `"install"` to a command that installs the CLI. It runs in the
   repository root through `bash -c` with a scrubbed environment (`env -i`).
   That does not hide the session's tokens from it on Linux; see
   [Secret isolation on Linux](#secret-isolation-on-linux).
4. Put the tool's access token in the provider environment, and list every
   name it can read in `exposure`.

### Token hygiene

- Use a dedicated config with non-production values.
- Make the token read-only and give it an expiry. Rotate it.
- Scope it to the smallest set of secrets the tasks need.
- Use a private provider environment. Do not share it with other repos.
- Keep a ruleset on `ffs/cloud/**` (see
  [Security model and stated limits](#security-model-and-stated-limits)).
  With secrets exposed, `doctor` fails without one, and the submit step
  checks the rules on the exact task branch.

The scanner and the runbook limit damage after a leak. They do not prevent
one.

## Recovery runbooks

Start every runbook with `status --run <run>`. Commands below assume the
main worktree.

### How Claude is launched

The real client (Claude Code 2.1.329) prints `Error: --cloud requires an
interactive terminal.` and exits non-zero when stdout is not a terminal, and
there is no documented headless way to create a cloud session. So the
dispatcher drives the interactive client through a pseudo-terminal (40 rows,
120 columns, `TERM` kept from the allowlist or set to `xterm-256color`). This
is undocumented provider behaviour and may break on a CLI update. Model and
effort are passed as `--model` and `--effort` and remain unverified.

The launcher only reads. It never writes a keystroke, so a first-run dialog
(for example "trust this folder") is never answered. Claude Code trusts a
folder per git root, so a fresh temporary clone always shows that dialog, even
under an already trusted parent directory. Set `FFS_CLOUD_CLONE_PARENT` (see
the settings table) to launch from the fixed clone path
`<parent>/ffs-cloud-launch-claude`, and run the interactive client once in that
exact directory to accept the trust dialog yourself. In the live runs `--cloud`
did show the dialog for a new clone. Before anything is started, a prompt
that would itself match the session id/URL detector, the not-a-terminal text
or the dialog detector is refused (`PROMPT_AMBIGUOUS`): the client echoes its prompt, and the
echo cannot be told apart from the client's own output. It reads until:

1. A `https://claude.ai/code/...` URL or a `session_...` / `cse_...` id appears
   (an id must contain a digit). Output is
   still captured for a grace period (`FFS_CLOUD_PTY_GRACE_S`, default 20 s) so
   the client can finish creating the session. The final decision also uses an
   id or URL found anywhere in the saved transcript, not only the live scan.
2. The client exits. If it printed the known error text (`CLI_EXIT NOT_A_TTY`)
   the task is `failed`. An exit with no session id/URL and no known error text,
   including exit 0, is NOT a failure: the client may have created the session
   before it ended. The task is `launched` with
   `launch_note: "exit-no-session-url"` (detail `CLI_EXIT NO_SESSION_URL`) and
   the orchestrator polls `claude/*` for a nonce-bound result until the normal
   deadline. (`uncertain` is not used for this: it gets only 3 recovery polls
   and then parks the task as `needs-operator`, long before a cloud session
   could push a result.)
3. The launch timeout passes with the client alive and no URL. The task is
   `launched` with `launch_note: "no-session-url"` and the orchestrator keeps
   polling `claude/*` for a nonce-bound result until the normal deadline. It
   needs no `adopt` and is never re-submitted. The exception is a recognised
   blocking dialog on screen (`CLI_EXIT BLOCKED_DIALOG`, state `failed`),
   because the client cannot get past it without a keystroke.

The client is then ended gently: the pseudo-terminal is closed (hang-up), FFS
waits up to 10 s, sends SIGTERM, and only then SIGKILL to the whole process
group. If the orchestrator itself is killed, the closed terminal hangs the
client up. The transcript, with escape sequences stripped and run through the
launch-log redaction, is written (capped, fsync'd) to the launch log on every
path, including a timeout. Both the in-memory capture and the 64 KB log keep the
start and the end of the transcript (16 KB of start, the rest the end, with a
marker between), so a late URL or error line survives a long redraw.

What the live client printed (Claude Code 2.1.329, after trust was accepted):
`Created cloud session: <title>`, a `View: https://claude.ai/code/session_...`
line and a `Resume with: claude --teleport session_...` line. It then exited
by itself, about 8 seconds after launch. FFS parsed the session id and the URL
from that output. The exact wording is the client's and may change.

### Runbook: stuck or uncertain launch

Symptoms: state `uncertain`, or `needs-operator` with `uncertain-launch`.
FFS never relaunches a launch it cannot confirm, because that could start a
second session.

1. Look in the provider UI (Claude Code on the web, Codex cloud tasks) for a
   session for this task.
2. Claude, session exists: the nonce-named branch should appear on origin.
   List it, then bind it:

   ```bash
   git ls-remote origin 'refs/heads/claude/*'
   bash scripts/gsd/cloud-dispatch.sh adopt --run <run> --task <task> --oid <oid>
   ```

3. Codex, session exists: copy its task id from the provider UI and run
   `adopt --run <run> --task <task> --remote-id <id>`. The task goes back to
   `launched` with a new deadline, and the normal poll and fetch follow.
   `adopt --oid` is refused for Codex. If you do not want that session, stop
   it in the provider UI, then `abandon --retry`.
4. No session exists: `abandon --retry` to start a new generation, or
   `abandon` to give up on the task.

`abandon` does not cancel a provider session. Stop it in the provider UI. A
late result of an old generation is dropped.

If a session failed in the cloud and pushed nothing, Claude gives no signal.
The task waits for `deadline_hours`, then ends `failed` with `deadline`.
Abandon it earlier to free its slot.

### Runbook: ambiguous candidates

Several `claude/*` refs qualify and none is the exact requested name. The
decision text lists them. A worker that ignores the requested branch name
leaves FFS with only "changed since launch" to go on.

1. Inspect them: `git ls-remote origin 'refs/heads/claude/*'`.
2. Other Claude web sessions on the same repository, including sibling tasks
   of this run, can push `claude/*` branches during the same window. Pick the
   right branch and `adopt --oid <oid>`.
3. If none is right, `abandon --retry`.

### Runbook: dependency change

Blocker `dependency-change`. The decision text lists files, added or changed
packages, removed packages, and other changed keys such as `scripts`.

1. Review the change in the candidate commit.
2. To accept: install the packages locally so your `verify.deps` directories
   match, record an `install:<pkg>` grant for each listed package, then run
   `resolve --action accept-deps`. The task re-runs collect with the change
   allowed.
3. To refuse: `resolve --action reject`.

The dependency file must itself be inside `owns`, or the result stops
earlier at `path-outside-owns`.

A change that FFS cannot attribute to named registry packages is
unattributed. It needs one more grant, `install:unattributed:<digest12>`,
where `<digest12>` is printed in the decision text. These changes are
unattributed: any lockfile change, a changed `scripts` key or another
install-time key in `package.json`, a package that does not come from the
registry by version range (git, URL or path sources), a change to the
manifest of an ecosystem FFS does not parse (`pyproject.toml`, `Cargo.toml`,
`go.mod`, `Gemfile*`, `pom.xml`, `build.gradle`, `build.gradle.kts`,
`Package.swift`, `deno.json`, `deno.jsonc`, `bunfig.toml`,
`environment.yml`, `NuGet.config`, `Podfile`, `mix.exs`, `pubspec.yaml` and
similar; any change to them is unattributed), an unparsable file, a repeated
declaration of one package in a manifest that changed, a change that only
removes dependencies, and a file that the worker touched in its history
without a net change. Removal-only changes name no package, so no
`install:<pkg>` grant can cover them: they need
`install:unattributed:<digest12>`. A change to a parsed manifest that
touches no dependency declaration and no install-time key, for example only
`version` in `package.json`, does not stop at `dependency-change` and needs
no grant. Read the decision text
for the list of files and reasons.

### Runbook: review unavailable

Blocker `review-unavailable`: the reviewer CLI is missing or not logged in,
timed out, printed no single verdict line, or the diff was over 2,000,000
bytes.

1. Fix the cause. For a large diff split the task.
2. `resolve --action retest` re-runs the review.

Each run of the review script spends one of two rounds from a durable
meter. After two rounds the next one returns `review-cap`. `resolve --action
retest` starts a fresh two-round meter, and it is allowed twice per task
generation. After that `resolve` refuses `retest`. Use
`resolve --action reject`, or `abandon --retry` for a new generation.

### Runbook: tests failed

State `rejected`, blocker `tests-failed`. Read
`runs/<run>/verify-<task>-g<N>.log`. Typical causes:

- The tests need network access or a live service. Mark the task `[local]`.
- A dependency directory is missing from `verify.deps`, or a toolchain is
  outside the `PATH` roots the sandbox re-opens (add `verify.read_roots`).
- The test timed out (`verify.timeout_s`).
- The result is wrong.

A rejected result cannot be retested. Fix the cause, then `abandon --retry`
for a new generation, which runs the worker again.

A run becomes `done` once every task is terminal. `adopt` and `resolve` are
refused on a `done` run (`RUN_DONE`), and so is a plain `abandon`. But
`abandon --retry` on a `rejected` or `failed` task makes the run `active`
again, and the next orchestrator start opens a new epoch. So you can retry the
last rejected task of a run in place.

### Runbook: orchestrator died

`status` shows `alive=false` and a growing `heartbeat-age-s`.

1. Read `runs/<run>/orchestrate.log`.
2. If reconcile is scheduled and works on your bash, it can restart the
   orchestrator. The lifecycle record allows 8 restarts after signal or
   out-of-memory kills (`env_deaths`) and none after other failures
   (`respawns` is 0).
3. Otherwise restart it yourself from a shell that has your exports:

   ```bash
   bash scripts/gsd/cloud-dispatch.sh orchestrate --run <run> --resume --detach
   ```

Cloud sessions keep running. The deadline keeps counting. Unfinished journal
entries are replayed on startup. The orchestrator stops with `STEP_FAILING`
(exit 78) if a step that is not about one task, such as the heartbeat or slot
handling, fails five ticks in a row. A step about one task that fails three
times in a row sends that task to `needs-operator` with `internal-error`, and
the other tasks carry on. Fix the cause shown in the log and restart.

### Runbook: parked run

A `CLOUD-PARKED` line means every remaining task needs you.

1. `status --run <run>` and read each `decision` line.
2. Act per [Blockers](#blockers) with `adopt`, `abandon` or `resolve`. Each
   one restarts the orchestrator.
3. If you are done, `abandon` the rest. The run then finishes.

### Runbook: secret leak

State `rejected`, blocker `secret-leak`. `status` prints this runbook, and
the same text is in `runs/<run>/collect-<task>-g<N>.log`. The findings list
pattern classes and locations, never values. FFS has not integrated the
result.

```text
A cloud result contained a secret-shaped value. 1. Revoke or rotate the
credential now (Doppler: `doppler configs tokens revoke`, then rotate the
leaked secret itself). 2. Delete the remote branch that carries it: `git
push <origin> --delete <branch>`. 3. Check the provider session transcript
and delete or unshare the session. 4. Re-mint the agent token and update the
provider environment. FFS has not integrated this result.
```

The scanner can also flag harmless text, for example a literal value
assigned to a variable named `password` in a test fixture. Treat a hit as a
leak first.

## Security model and stated limits

- Results are untrusted. A result passes the scan, path, dependency, review
  and sandbox gates before it reaches the integration ref. These reduce risk.
  They do not prove a result is safe. Read the integration branch before you
  merge it.
- The nonce is correlation, not authentication. Anyone with push access who
  reads a published manifest can copy it. FFS therefore accepts any content
  that matches `(run, task, generation, base)` and independently passes
  every gate, binds the first OID seen unchanged on two consecutive polls,
  and ignores later candidates.
- A worker that holds secrets can print or push them before FFS scans. Scope,
  expiry, non-production values, the inbound scan and the leak runbook bound
  the damage.
- The guard hook is fail-open and anti-accident only.
- Model and effort are unverified in the cloud. Claude gets `--model` and
  `--effort`, but whether the client applies them to `--cloud` at all is
  unconfirmed, and it may still run something else. Codex gets neither.
- Verification evidence from the worker (`env_report`) is self-reported. The
  ledger marks it `env_report_source: worker`. FFS has no independent channel
  into the cloud machine.
- The `SessionStart` hook runs code from the checked-out branch. FFS starts
  sessions only on task branches it created from your base or from an
  integration commit that passed the denylist, and it checks the branch OID on
  origin just before each launch. Anyone who can push to the repository could
  move a task branch between that check and the provider's clone. The
  control is server side, below. A result built on a tampered branch is
  rejected by the denylist, but that does not undo the session having run.
- For Claude, FFS starts `claude --cloud` from a fresh single-branch clone of
  the task branch, so the provider's upload path sees only that branch. The
  launch is not a plain non-interactive exec: see
  [How Claude is launched](#how-claude-is-launched).
- The orchestrator sleeps with the host. Local review and tests remain local
  load. Mixed groups do not offload. FFS cannot choose the Claude cloud
  environment. Sandboxed tests cannot reach the network.
- `doctor` probes the CLI logins on a best-effort basis only. A missing,
  failing or timed-out probe is `WARN LOGIN_UNVERIFIED`, so a green `doctor`
  does not prove you are logged in.
- When a Claude worker does not push to the requested branch name, FFS falls
  back to refs that are new or changed since launch. Another Claude session
  on the same repository can then be bound to the wrong task. FFS never binds
  a ref named for another task of the same run. If the bound ref has a
  missing or invalid manifest (which includes a wrong nonce), FFS records it
  as rejected and goes back to polling. The exception is a ref that is
  provably the task's own (its exact expected branch name, or the task's
  nonce in the manifest) with a present but invalid manifest: that goes to
  `needs-operator` with `manifest-invalid`.
- A task in `needs-operator` or `uncertain` keeps its provider slot until its
  launch deadline. The orchestrator then releases the slot once. Until then
  the capacity for other tasks is smaller. A slot of a missing or `done` run
  is dropped at once, and any slot is dropped one hour after its deadline.
- A recovered live task always holds a slot. After an unbind (a bound
  result that was not this task's, so the task goes back to `running`) or
  after `adopt --remote-id`, the task is still running in the cloud, so its
  slot is taken even above the provider or total limit. New admissions wait
  until it ends.
- A lone `parked` run keeps its expired slot until any orchestrator resumes or
  `cleanup` runs. Slots are reaped only in an orchestrator tick and in
  `cleanup`. Workaround:
  `bash scripts/gsd/cloud-dispatch.sh orchestrate --run <run> --resume`.
- Running `cleanup` during a concurrent `start` of the same run id can delete
  that run. `cleanup` also removes a run that is not `done`, has no task inputs file
  and has no task that ever started.

Branch rulesets (GitHub):

How a GitHub ruleset on `refs/heads/ffs/cloud/**` that blocks updates and
deletions affects FFS's pushes of new task branches and the provider's clone
is per GitHub's documentation at the time of writing. Confirm it in your own
repository before you rely on it, preferably on a throwaway repository first.

- `ffs/cloud/**`: create an active ruleset whose target is exactly
  `refs/heads/ffs/cloud/**` (the pattern `doctor` recognizes, besides
  `~ALL` and `refs/heads/ffs/**`). Enable the rules that restrict updates and
  deletions, and leave creation unrestricted so FFS can push new task
  branches. Keep only the repository admin role in the bypass list;
  `RULESET_BYPASS` fails otherwise. With any exposed secret, `doctor` fails
  without this ruleset, and every submit re-checks the active `update` and
  `deletion` rules on the exact branch.
- `claude/**`: FFS does not check anything here. The Claude worker creates its
  result branch and then updates it, so do not block updates the way you do
  for `ffs/cloud/**`. Treat every branch there as untrusted input; that is
  why the nonce and the gates exist. Any tighter rule, such as limiting who
  may push, depends on the identity the provider pushes as, which is
  undocumented by the provider. Test on a throwaway repository first.

## Live verification

The Claude backend ran live on 2026-11-20 with Claude Code 2.1.329. The task
was one trivial docs change. The runs drove `cloud-dispatch.sh` directly.

What was seen:

- `claude --cloud "<prompt>"` refuses to run without an interactive
  terminal. The Claude backend starts it on a pseudo-terminal. This is
  undocumented provider behaviour and can break on a client update. See
  [How Claude is launched](#how-claude-is-launched).
- The client showed its folder trust dialog for the launch clone. FFS never
  answers it, so a launch that meets it ends `failed` (`CLI_EXIT
  BLOCKED_DIALOG`) and creates no session. After trust was accepted (see
  [Quick start](#quick-start), step 5) the launch took about 8 seconds. The
  client printed the session title, a `View:` URL and a `Resume with:` line,
  then exited by itself. FFS parsed the session id and URL.
- The cloud worker finished the task and pushed
  `claude/ffs-<run>-<task>-<nonce8>` about 2.5 minutes after launch. The
  committed `SessionStart` provisioning ran: the manifest `env_report` listed
  tool versions.
- The orchestrator was killed while it polled the task. One
  `orchestrate --resume` produced no second launch (launch count 1,
  generation 1).
- The first attempt failed at the manifest check. The worker wrote `tests_run`
  items as strings, and the validator requires objects `{"cmd": ..., "rc":
  ...}`. The packet had not stated that shape. The worker contract now states
  it. A result that fails this way on the task's own branch now ends in
  `needs-operator` with `manifest-invalid`; see [Binding](#binding).
- The next run passed every gate: nonce binding, isolation, path and secret
  checks, review by the opposite family (verdict `APPROVE`, recorded with the
  diff sha and the patch id), the sandboxed verify command (rc 0), and
  integration to the local ref `ffs/cloud/<run>/integration`. Launch to
  `integrated` took under 3 minutes.

What was not verified live:

- The Codex backend. No environment id was supplied.
- Whether `--model` and `--effort` take effect under `--cloud`. They are
  still recorded as requested and unverified.
- More than one task, and dependencies between tasks.
- The installed `/swarm --cloud` skill path.
- Secrets modes.
- Restart driven by `reconcile`.
- A client stuck on a screen other than the trust dialog.

Cleanup findings from the same runs:

- `cleanup --remote` does not delete the provider-created `claude/ffs-*`
  branch. Delete it by hand.
- A run whose task never binds stays `active`, and `cleanup` refuses it until
  you `abandon` the task.
- The review output itself is not kept in the run directory. Only the verdict
  record is in the ledger.

## Known limits and residual risks

These limits stay in this release. Read them before you expose secrets or run
unattended.

- The nonce is correlation, not authentication. Anyone with push access who
  reads a published manifest can copy it. The other gates are what stop a
  copied result.
- A worker that holds a secret can print or push it before FFS scans. The
  scan runs after the worker has finished. Scope, expiry, non-production
  values and the [leak runbook](#runbook-secret-leak) bound the damage.
- Encoded secrets can evade the scanner. It matches known secret patterns and
  secret-named assignments in the raw bytes. It does not decode base64 or
  other encodings, so an encoded or split value passes.
- The operator-only guard fails open. It is an anti-accident control, and
  indirection through script files or imports bypasses it.
- Model and effort are unverified in the cloud. Claude gets `--model` and
  `--effort`, but whether `--cloud` honours them is unconfirmed. Codex gets
  neither.
- On Linux, `env -i` is the only secret isolation in `setup.sh`, and a
  dependency installer can read `/proc/<pid>/environ` of its ancestors. See
  [Secret isolation on Linux](#secret-isolation-on-linux).
- A double-fork child of a sandboxed test that no process-table poll saw
  while its parent chain was alive is not swept. See
  [Sandboxed tests](#sandboxed-tests).
- The sandbox allows one Mach service,
  `com.apple.system.opendirectoryd.libinfo`, because user lookups need it.
  Whether it can carry DNS lookups for outside names is not tested.
- The JWT scanner pattern needs a non-token character (or the start of the
  text) before `eyJ`. A JWT directly after `-` is not matched.
- Live coverage is thin. Only the Claude backend ran live, with one trivial
  task. Everything else is tested against fake provider CLIs and injected
  fakes, so treat provider behavior not listed in
  [Live verification](#live-verification) as unverified until you have seen it
  work in your own account.
- The review output is not kept in the run directory. Only the verdict
  record (family, verdict, diff sha, patch id, tree) is in the ledger.
- `cleanup --remote` does not delete the provider-created `claude/ffs-*`
  branch. Delete it by hand.
- A process that runs as your user at the same time is outside the threat
  model. The dispatcher checks names and then acts on them (the fixed launch
  clone and its checkout, staging recovery, the candidate ref). A second
  process with your user id that renames a directory or edits a ref between
  the check and the action can make the dispatcher write to or remove the
  replacement. Such a process can already delete your files directly. Do not
  run other tools against the clone parent or the `ffs/cloud/*` refs while a
  run is active.
- Staging recovery removes an empty `.ffs-cloud-new-*` entry or an empty
  marker file in the clone parent when you own it. Keep nothing of your own
  under those names.
- `cleanup` keeps a task branch whose creation the ledger did not confirm.
  This includes a branch the run itself made when the first push failed and a
  retry succeeded, or when the process died right after the push. The ref is
  printed as `CLOUD:cleanup-kept ... (creation not confirmed)`. Delete it by
  hand before you reuse the run id.

## Known deviations from the design contract

The design contract is `docs/cloud-delegation-contracts.md`. Section 14 and
section 15 of it override earlier sections. Where the code differs from the
contract, this page documents the code. Differences found:

- `doctor` cannot confirm an OAuth login. It runs a best-effort probe and
  reports `WARN LOGIN_UNVERIFIED` when the CLI has no probe subcommand or the
  probe fails.
- Exit 75 is returned only by `orchestrate --wait-lock` when the lock stays
  held. A contested ledger lock blocks instead.
- `adopt --remote-id` binds a Codex session id. It is not in the design
  contract. `adopt --oid` is refused for Codex tasks.
- `cleanup --discard-unmerged` and the `INTEGRATION_UNMERGED` check are not in
  the contract.
- `cloud-review.sh` takes six arguments with the generation last, and an
  optional seventh `attempt`. It prints a final `CLOUD-REVIEW:` line.
  `resolve --action retest` on a review blocker starts a fresh two-round
  meter, up to two times per task generation.
- The blockers `scan-refused`, `branch-conflict`, `push-failed`,
  `internal-error`, `integration-ref-foreign` and `binary-change` are not in
  the contract's blocker table. The code adds them. The `case-collision`
  blocker name was removed; case-colliding paths are reported as `denylist`
  at collect and as `merge-conflict` at integration.
- The blocker `candidate-moved` and the state `superseded` are defined but
  never produced.
- `abandon --retry` of a `rejected` or `failed` task reopens a `done` run.
  `abandon` of an `integrating` task is refused (`INTEGRATION_IN_PROGRESS`).
- The candidate commit has the task base as its only parent and a fixed
  identity. The contract describes a commit on top of the worker's result.
- Integration merges only disjoint paths, through a temporary index with no
  worktree, and requires an exact expected tree. The contract describes a
  merge in a temporary worktree with conflict detection. The reviewed patch
  id is computed with `--verbatim`, not `--stable`.
- An `uncertain` Claude launch gets 3 recovery polls and an `uncertain` Codex
  launch gets 1, where the contract allows one re-parse or one tick.
- A Claude candidate with a missing or invalid manifest is unbound and the
  task goes back to polling, unless the candidate is provably this task's own
  result (exact expected branch name, or this generation's nonce) and the
  manifest is present but invalid. That case is `needs-operator` with
  `manifest-invalid`.
- Operator verbs start `orchestrate --resume --wait-lock 300` when the lock is
  held, not a 30 second wait.
- The sandbox profile is stricter than the contract's section 14.7. It denies
  more Mach lookups, Launch Services, signals outside the sandbox, the
  operator temp tree and many credential locations. It grants a toolchain bin
  directory or a known layout root, not the parent of a `PATH` entry. It has a
  startup self-check and an 8 MB redacting log writer.
- `secrets.network_secret_hosts` is in the template and the contract but no
  code reads it.
- `env init` does not fill the Doppler pins in `setup.sh`, and the template
  `secrets.wrapper` is empty, where the contract shows `doppler run --`.
- `scan.allow` also accepts `{"path", "class"}` entries, which the contract
  does not list. A path entry is what allows a `secret-filename` finding.
- Any binary blob in outbound history is a finding.
- `doctor` accepts only three literal ruleset include patterns.
- The sandbox refuses any base tree that contains a symlink or submodule, not
  just changes made by the worker.
- The contract allows exactly three differences without the flag, and so does
  the code. The `autonomy-grant`, `preflight` and `spec-status` skill texts
  gained cloud notes, and `spec-status` prints its `== CLOUD ==` section only
  when a cloud run ledger exists. None of that changes behavior unless cloud is
  used.

## Not in this release

- `--cloud` on `/feature-implement` and `/task-swarm`. They arrive later.
- API-key backends. They are refused (`API_KEY_BACKEND`).
- Merging, pushing the integration branch for you, and deploying.
- Result tests on Linux. The sandbox needs macOS `sandbox-exec`.
- Non-GitHub remotes.
- Cancelling provider sessions from FFS.
- Selecting or verifying the Claude cloud environment from the CLI.
- Verified Codex output formats. The parsers for `codex cloud` status and
  diff output were written against guessed shapes and are meant to be
  replaced by recorded fixtures from a live Codex run. The Claude launch
  output was seen live once (Claude Code 2.1.329); see
  [Live verification](#live-verification).
