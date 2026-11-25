# How to run your first cloud delegation

This guide takes you from a repository that has never used `/swarm --cloud`
to a first dry run, then a first real run, and then to a merged result.

Cloud delegation is experimental. The Claude backend ran live once, on one
trivial docs task (Claude Code 2.1.329, 2026-11-20). The Codex backend,
`--model` and `--effort` under `--cloud`, runs with more than one task, the
secrets modes and the installed `/swarm --cloud` skill path have not been
verified live. Treat any provider behavior described here that is not listed
under [What was verified live](#what-was-verified-live) as unverified until you
have seen it work. For the full reference, see
[Cloud delegation](cloud-delegation.md). For secrets, see
[How to give cloud sessions secrets with Doppler](howto-cloud-secrets-doppler.md)
and [How to give cloud sessions secrets without Doppler](howto-cloud-secrets-other.md).

## Before you begin

You need all of these:

- macOS. Result tests run under `sandbox-exec`, and `doctor` fails without it.
- Claude Code and/or Codex CLI, logged in with OAuth. `ANTHROPIC_API_KEY` and
  `OPENAI_API_KEY` must be unset. API-key backends are refused with
  `API_KEY_BACKEND`.
- A GitHub `origin` remote whose URL carries no credentials. Use SSH or a
  credential helper.
- Your current commit pushed to a remote branch, and no modified tracked
  files. Untracked files are allowed.
- The `gh` CLI, authenticated. `doctor` uses it to read repository rulesets.
- To run from the main worktree. A linked worktree is refused with
  `NOT_MAIN_WORKTREE`.
- For Claude: a directory outside your repository, owned by you, where you can
  clone the repository and run `claude` once in a real terminal. Step 3 explains
  why.

`--cloud` works on `/swarm` only. `/feature-implement` and `/task-swarm` print
one line saying cloud delegation for them arrives in a later release, and stop,
when given `--cloud` (any form) or when `FFS_CLOUD` is set. Unset `FFS_CLOUD`
before you use those two skills.

The commands below use the path `scripts/gsd/cloud-dispatch.sh`, as the
`/swarm` skill does. A user-scope install also keeps a copy under
`~/.claude/lib/feature-fix-swarm/scripts/gsd/cloud-dispatch.sh`.

Every verb takes `--root ABS`, the absolute path of the main worktree. The
default comes from the current directory when `--root` is not given at all; an
explicit empty `--root` (for example an empty command substitution) is `BAD_ROOT`,
never read as the current directory. For `env init`, put `--root` after `init`.

## Steps

### 1. See what is missing

```bash
bash scripts/gsd/cloud-dispatch.sh doctor
```

Exit 0 means no `FAIL`. Exit 3 means at least one check failed. On a fresh
repository the first run is expected to be not green. `CLOUD_FILES` fails
because `.ffs/cloud/` is not in your base commit yet. `BASE_PUSHED` fails if
your commit is not on a remote. Do not fix anything yet. Go to step 2 and run
`doctor` again at step 5.

### 2. Write the consumer files and commit them

```bash
bash scripts/gsd/cloud-dispatch.sh env init --register-hook
```

This writes `.ffs/cloud/config.json`, `setup.sh`, `env-check.sh` and
`session-start.sh`. `--register-hook` also merges a `SessionStart` entry into
`.claude/settings.json`. Existing files are kept unless you pass `--force`.
Nothing is committed for you. The command prints a `git add` line with the
files it wrote.

Edit `.ffs/cloud/config.json` and set `verify.cmd` to your test command. A task
with no verify command always runs locally.

Then commit and push `.ffs/cloud/*` and, if you used `--register-hook`,
`.claude/settings.json`. `doctor` reads these files from the commit, not the
working tree.

### 3. Create the provider environment

FFS cannot create or check a provider environment. `env-spec` prints the
one-time steps for each provider you use:

```bash
bash scripts/gsd/cloud-dispatch.sh env-spec --provider claude
bash scripts/gsd/cloud-dispatch.sh env-spec --provider codex
```

Do those steps in the provider's UI. Use a private environment. If you expose
any secret name, list every reachable name in
`.ffs/cloud/config.json` under `secrets.exposure.<provider>`. Start with none:
the default secrets mode is `none`.

`env-spec` also asks for a GitHub ruleset on `ffs/cloud/**` that blocks updates
and deletions for non-admins. `doctor` makes this a `FAIL` when any secret name
is exposed, and a `WARN` otherwise.

For Codex, export the environment id in the shell that runs the dispatcher.
The variable name is `codex.env_id_var` in the config:

```bash
export FFS_CODEX_CLOUD_ENV=<environment id>
```

Without it, Codex is not usable.

#### Claude only: accept the folder trust dialog once

The real `claude --cloud` refuses to run unless it has a terminal, so FFS starts
it on a pseudo-terminal that it only reads from. This is undocumented provider
behavior and can break when the client updates. The client shows a folder trust
dialog for the folder it starts in. Trust is per git root, so a trusted parent
directory does not cover a new clone. FFS never answers the dialog, so you
accept it once yourself, in a folder FFS then reuses:

1. Choose a directory outside the repository that you own and that is not a
   symlink. Export it in the shell that runs the dispatcher, before `start`.
   FFS then clones to `<parent>/ffs-cloud-launch-claude`:

   ```bash
   mkdir -p <parent>
   export FFS_CLOUD_CLONE_PARENT=<parent>
   ```

2. Clone your repository to that exact path:

   ```bash
   git clone <origin url> <parent>/ffs-cloud-launch-claude
   ```

3. In a real terminal, run the interactive client in that directory, accept the
   trust dialog, then quit:

   ```bash
   cd <parent>/ffs-cloud-launch-claude && claude
   ```

4. Optional check. This shows the client's first screen and creates no session.
   `dialog` should be `false`:

   ```bash
   bash scripts/gsd/cloud-dispatch.sh probe-claude --dir <parent>/ffs-cloud-launch-claude
   ```

5. After you accept the trust dialog, remove the clone:

   ```bash
   rm -rf <parent>/ffs-cloud-launch-claude
   ```

   Your clone has no ownership marker, so FFS refuses it with
   `CLONE_PATH_INVALID` and never deletes it. Each launch then creates the
   directory in a private `.ffs-cloud-new-*` staging directory, writes the marker
   `<parent>/ffs-cloud-launch-claude.ffs-owned`, publishes the directory with a
   no-replace rename, and removes it after the launch. See
   [Cloud delegation](cloud-delegation.md#quick-start) for the details.

Without `FFS_CLOUD_CLONE_PARENT` the clone is a new temporary directory each
time, which always shows the dialog, and the launch ends `failed` (see
[Launch problems](#launch-problems)). `FFS_CLOUD_PTY_GRACE_S` (0 to 120, default
20) sets how many seconds FFS keeps reading after the session URL appears.
`--model` and `--effort` are passed to the client, but whether `--cloud` honors
them is not verified.

### 4. Preview the routing

Run a dry run. Annotate tasks so the router can place them:

```text
/swarm --dry-run --cloud \
  "Add retry with backoff to the HTTP client [owns: src/client/**] [est: 45m] [verify: npm test -- client]" \
  "Reword the README intro [owns: README.md] [est: 5m] [verify: true]"
```

The dry run runs `doctor` and `plan`, prints groups, routes, providers,
reasons and missing grants, and stops. It starts nothing.

If nothing routes to the cloud, read the reason codes in the plan:

- `VERIFY_CMD_MISSING`: the task has no `[verify: ...]` and `verify.cmd` in the
  config is empty. A whitespace-only command counts as empty, so a blank
  command never reaches `verified`.
- `BELOW_THRESHOLD`: no task in the group has an estimate at or above the
  threshold. The threshold is 20 minutes for `auto` (the default), 5 minutes
  for `prefer`, and 0 for `required`. Add `[est: ...]`, or use
  `--cloud=prefer`.
- `OWNS_MISSING`: the task has no `[owns: ...]`.
- `GROUP_HAS_INELIGIBLE_TASK`: one task in a connected group cannot go to the
  cloud, so the whole group stays local.

You can also run the planner without `/swarm`. The tasks file is a JSON list
of annotated strings:

```bash
bash scripts/gsd/cloud-dispatch.sh plan --tasks tasks.json --policy auto --with-doctor
```

### 5. Check again

```bash
bash scripts/gsd/cloud-dispatch.sh doctor
```

Fix every `FAIL`. `WARN` lines are advisory.

### 6. Grants

A run needs these grants before it can start:

- `cloud-dispatch:<provider>` for each provider it uses, for example
  `cloud-dispatch:claude`.
- `push:origin/ffs/cloud/<run>`.
- `secret-use:<NAME>` for each exposed secret name. None, in `none` mode.

In an interactive `/swarm` session, drop `--dry-run`. `/swarm` shows the task
list, providers and every exposed secret name, and asks one question. On yes it
records the grants for you. Autonomous and headless runs never grant for
themselves. They need grants that already exist, and `start` refuses when one
is missing.

To grant by hand, use the same command `/swarm` runs. Use the run id that
`/swarm` reports, or pick one yourself that matches
`^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$`. Run it from the repository you pass as
`--root` (the main worktree), because the dispatcher reads that repository's
grant store (`.feature-fix-swarm/evidence.json` unless `GATES_STORE` says
otherwise), never the directory you happen to be in. A grant recorded in another
checkout is not seen. Use `gates.py` from an FFS checkout, or the copy in the
managed lib directory:

```bash
cd <root>   # the repository passed as --root
python3 lib/gates.py grant <run> \
  --action cloud-dispatch:claude \
  --action push:origin/ffs/cloud/<run> \
  --ttl-hours 24 --reason "first cloud run"
```

Grants expire. An expired grant stops the next admit or submit.

### 7. Run it for real

Run the same `/swarm ... --cloud` command without `--dry-run`. Starting a run
prints lines such as `CLOUD:started run=<run> tasks=N` and
`CLOUD:orchestrator-started pid=N`. A detached orchestrator then launches,
polls and collects results. Use `--cloud=required` only if you want the run to
stop when a group cannot go to the cloud.

### 8. Read the status

```bash
bash scripts/gsd/cloud-dispatch.sh status --run <run>
```

The output has one `CLOUD:status` line, one `CLOUD:task` line per task, and a
`CLOUD:decision` line for each decision a worker or gate left for you. Add
`--json` for one JSON document. Exit 0 means no task needs you. Exit 4 means at
least one task is `needs-operator` or `uncertain`.

### 9. Handle `needs-operator`

`needs-operator` means FFS stopped on a task and will not guess. Each task
shows a `blocker`. The orchestrator parks the run when every remaining task
needs you, and prints `CLOUD-PARKED run=<run> items=N`.

There are three operator verbs. Run them in your own terminal. The
operator-only guard hook denies them in agent shells, and the `/swarm` skill
never runs them:

```bash
# Bind a result FFS could not find on its own.
bash scripts/gsd/cloud-dispatch.sh adopt --run <run> --task <task> --oid <oid>          # Claude
bash scripts/gsd/cloud-dispatch.sh adopt --run <run> --task <task> --remote-id <id>     # Codex

# Give up on a task, or start a new generation of it.
bash scripts/gsd/cloud-dispatch.sh abandon --run <run> --task <task> [--retry]

# Clear a blocker after you fixed its cause.
bash scripts/gsd/cloud-dispatch.sh resolve --run <run> --task <task> --action retest|accept-deps|reject
```

Use `adopt` for `uncertain` tasks and for the blockers `uncertain-launch`,
`poll-errors` and `ambiguous-candidates`. `--oid` is for Claude and
`--remote-id` is for Codex. Use `resolve` for blockers such as `grant-missing`
(grant it, then `resolve --action retest`), `dependency-change` (`accept-deps`
or `reject`) and `review-unavailable`. Use `abandon --retry` for `rejected` or
`failed` tasks. `abandon` does not stop the provider session. Stop it in the
provider UI.

Four blockers are newer and have narrower exits:

| Blocker | What happened | Exit |
|---|---|---|
| `review-model-downgraded` | The review approved, but a weaker model than the requested judgment-tier model answered. | `resolve --action retest` or `reject` |
| `review-model-unknown` | The review approved, but the answering model could not be determined. | `resolve --action retest` or `reject` |
| `verify-cmd-missing` | The verify command was blank when verification ran. No tests ran. The router already refuses such a task, so this is a defensive stop. | `resolve --action reject` or `abandon --retry` |
| `manifest-invalid` | The result manifest on the task's own branch failed validation. The detail names the validator code only. | `abandon [--retry]`. A corrected manifest pushed later is not picked up. |

The two review blockers share the two-retest limit of the other review
blockers. The full blocker table and the recovery runbooks are in
[Cloud delegation](cloud-delegation.md#blockers).

### 10. Merge the result yourself

The result is a local branch, `ffs/cloud/<run>/integration`. FFS never merges
it into your branch and never touches your working tree. Review it, then merge:

```bash
git log --oneline ffs/cloud/<run>/integration
git merge --no-ff ffs/cloud/<run>/integration
```

### 11. Clean up

When the run is `done` and you have merged the integration branch:

```bash
bash scripts/gsd/cloud-dispatch.sh cleanup --run <run> [--remote]
```

`cleanup` deletes only the `ffs/cloud/<run>/*` refs the ledger accounts for, each
only at its recorded OID, then the run state. A ref with no recorded OID is kept
and printed `CLOUD:cleanup-kept ref=... (reason)`. A ref the ledger has no proof this run created is also kept, as
`(creation not confirmed)`. A local task branch is journaled as created right after the run makes it, so
it is cleaned up even after a failed push and retry. What can still be kept: a remote branch whose
creation acknowledgement was lost, a remote branch another clone created first, and a local branch
when the process died before journaling it. Delete such a ref by hand before reusing the run id.
A ref that moved ends
`CLEANUP_INCOMPLETE` and keeps the ledger so a rerun can finish. `--remote`
deletes the pushed task branches with a lease on the recorded OID, never a
remote `integration` branch, and needs the push grant. It never deletes the
`claude/ffs-*` branch that the Claude worker created. Delete it yourself with
`git push origin --delete <branch>`.
`cleanup` also refuses a run that is not `done` (`RUN_NOT_DONE`), except a run
whose `start` crashed before any task began. A run whose
task never bound a result stays `active` until you `abandon` that task. `--discard-unmerged` skips the unmerged-work check. Use
it only when you want to throw the result away.

## Verification

You have a working setup when:

1. `doctor` prints `CLOUD:doctor green` and exits 0.
2. The dry run prints a plan, shows at least one group routed to the cloud,
   and starts nothing.
3. After a real run, `status` shows each cloud task as `integrated` and the run
   as `done`. Check:

   ```bash
   bash scripts/gsd/cloud-dispatch.sh status --run <run> --json
   ```

4. `git log --oneline ffs/cloud/<run>/integration` lists the published
   commits.
5. After `cleanup`, this prints nothing:

   ```bash
   git for-each-ref "refs/heads/ffs/cloud/<run>/"
   ```

## Troubleshooting

`CLOUD-REFUSED: <CODE> <detail>` is printed on stdout and exits 3. A usage
error prints `<prog>: error: ...` or `CLOUD-ERROR:` on stderr and exits 2 or
78 (see the code lists below). `STORE_IO` is printed as `CLOUD-REFUSED:` but
exits 78.

Refusals (`CLOUD-REFUSED`, exit 3):

| Code | Meaning and fix |
|---|---|
| `NOT_A_REPO` | There is no git worktree at the root. Run from your repository. |
| `NOT_MAIN_WORKTREE` | You are in a linked worktree, or `--root` is not the top level. Use the main worktree. From a linked worktree `/swarm` passes its own top level as `--root` to `plan` and `start`, so they are refused here, and its `status` calls read the main worktree's ledger (`LEDGER_MISSING` for a first run, which then goes local under `auto` and `prefer`). |
| `NO_ORIGIN` | The repository has no `origin` remote. Add one. |
| `ORIGIN_HAS_CREDENTIALS` | The `origin` URL embeds a user and password or token. Use a credential helper or SSH. |
| `ORIGIN_URL_INVALID` | The `origin` URL is not usable. |
| `ORIGIN_CHANGED` | `origin` no longer matches the URL pinned at start. |
| `DOCTOR_NOT_GREEN` | `doctor` has a `FAIL`. The detail lists them. See the checks in [Cloud delegation](cloud-delegation.md#doctor-checks). |
| `LIFECYCLE_DISABLED` | `FFS_LIFECYCLE=off`. Unset it. |
| `MANAGED_INGRESS` | `FFS_MANAGED_INGRESS` is set and not `0`. The run stays local. |
| `TOKEN_BUDGET` | `GSD_TOKEN_BUDGET` is set. The run stays local. |
| `DIRTY_BASE` | Tracked files are modified. Commit or stash them. |
| `NO_BASE` | The repository has no commit at HEAD. |
| `RUN_EXISTS` | The run id already has a ledger, usually a retry after a lost acknowledgement. The tasks may already be running. Read `status --run <run>`; do not start them under a new id. The line ends `batch=same` (optionally `local-claimed=<ids>`), `batch=other` or `batch=unknown`, and may add `orchestrator=restarted` or `orchestrator=restart-failed cause=<ERRNO>` (the restart spawn raised `OSError`; `cause` is the errno name, or `OSERROR` when there is none, never the message). After `restart-failed`, `/swarm` reports the cause and stops: it runs no `reconcile.sh`, no local task and no cloud task. Fix the cause, then repeat the identical `start` once. |
| `RUN_NOT_INITIALIZED` | `orchestrate` found a run with no initialization record. Retry the identical `start`; a run from an earlier build cannot be finished. |
| `NO_CLOUD_TASKS` | Nothing routes to the cloud. Read the reason codes in step 4. |
| `CLOUD_REQUIRED_NOT_ELIGIBLE` | Policy is `required` and a group cannot go to the cloud. Fix the task, or use `auto` or `prefer`. |
| `GRANT_MISSING` | A `cloud-dispatch:<provider>` or `push:origin/ffs/cloud/<run>` grant is missing. Grant it (step 6). |
| `EXPOSURE_NOT_GRANTED` | A `secret-use:<NAME>` grant is missing. Grant it, or empty `secrets.exposure`. |
| `GRANT_STORE_ERROR` | `gates.py check-grant` failed in an unexpected way. |
| `SECRET_IN_PROMPT`, `SECRET_IN_PACKET` | The task text or worker packet matched the secret scanner. Remove the secret-shaped text. For `/swarm`, `SECRET_IN_PROMPT` from `plan` or `start` ends the invocation: nothing runs, cloud or local, under `auto`, `prefer`, `required` and `--dry-run`. |
| `PACKET_INVALID` | The worker packet could not be built, for example an invalid `secrets.wrapper`. |
| `RUN_NOT_DONE` | `cleanup` only removes `done` runs. |
| `CLEANUP_INCOMPLETE` | Some refs moved off their recorded OID or could not be deleted. The ledger is kept. Fix the cause (delete a foreign remote ref by hand) and rerun `cleanup`. |
| `INTEGRATION_UNMERGED` | `cleanup` found work on the integration branch that no other branch has. Merge it, or pass `--discard-unmerged`. |
| `INTEGRATION_IN_PROGRESS` | The task is `integrating`. Retry after the orchestrator's next tick. |
| `RUN_DONE` | The run is `done`. `adopt`, `resolve` and a plain `abandon` are refused. |
| `STALE_APPROVAL` | The generation, bound result or dependency report changed after the grants were checked. Run the verb again. |
| `DEPENDENCY_REPORT_MISSING`, `DEPENDENCY_REPORT_INVALID` | `accept-deps` cannot use the dependency report. Use `abandon --retry`. |
| `ADOPT_UNSUPPORTED_PROVIDER` | `--oid` was used for Codex, or `--remote-id` for Claude. |
| `REMOTE_UNREACHABLE` | `ls-remote` failed while listing remote branches. Check your network and credentials. |
| `STORE_IO` | A filesystem failure. Printed as `CLOUD-REFUSED:` with exit 78. |

Usage errors (`CLOUD-ERROR` or argparse text on stderr, exit 2):
`BAD_RUN_ID`, `BAD_TASKS_FILE` (the tasks file must be a non-empty JSON list),
`BAD_POLICY`, `BAD_PROVIDER`, `BAD_ADOPT` (pass exactly one of `--oid` and
`--remote-id`), `BAD_OID`, `BAD_REMOTE_ID`, `BAD_ROOT` (`--root` is empty or not
an absolute path).

Store and internal errors (`CLOUD-ERROR`, exit 78) include `TEMPLATES_MISSING`
(no templates next to the library; reinstall), `SETTINGS_INVALID` (`.claude/settings.json`
is not a JSON object), `HOOK_API_MISSING` and `INTEGRATION_REF_UNCONFIRMED` (an identical
retry of a crashed `start` found the integration ref it left behind and cannot tell it is its
own). `start` prints it on stderr and exits 78. `/swarm` relays it, runs no task (cloud or
local) and stops. Inspect the ref the message names before another `start`: for a symbolic
ref, remove only the symbolic ref (`git symbolic-ref --delete <ref>`); for a plain ref, the
message prints `git update-ref --no-deref -d <ref> <oid>`.

### Launch problems

A launch that fails leaves the task in `failed`. The orchestrator log
`.feature-fix-swarm/cloud/runs/<run>/orchestrate.log` has a line
`launch <task> g<N>: failed <detail>`, and the redacted client transcript is in
`launch-<task>-g<N>.log` next to it. Fix the cause, then
`abandon --run <run> --task <task> --retry`. Claude launch details:

| Detail | Meaning and fix |
|---|---|
| `CLI_EXIT BLOCKED_DIALOG` | The client stopped at a first-run dialog, usually the folder trust dialog. No session was created. Set `FFS_CLOUD_CLONE_PARENT` and accept trust once in `<parent>/ffs-cloud-launch-claude` (step 3). Check with `probe-claude --dir`. |
| `CLI_EXIT NOT_A_TTY` | The client exited with its "requires an interactive terminal" text. The pseudo-terminal launch may not work with this client version (it relies on undocumented client behavior). Run `probe-claude --dir <path>` and see [How Claude is launched](cloud-delegation.md#how-claude-is-launched). |
| `PROMPT_AMBIGUOUS` | The task prompt would itself look like a session id or URL, the not-a-terminal text or a dialog. The client echoes its prompt, so FFS refuses before anything starts. Reword the task text. |
| `CLONE_PARENT_INVALID <reason>` | `FFS_CLOUD_CLONE_PARENT` is unusable. The reason is `NOT_ABSOLUTE`, `MISSING`, `SYMLINK`, `NOT_A_DIRECTORY`, `NOT_OWNED` or `INSIDE_REPO`. There is no fallback to a temporary directory. |
| `CLONE_LOCK_BUSY` | Another launch held the lock on `<parent>/.ffs-cloud-launch-claude.lock` for the whole wait. Rerun after it ends. |
| `CLONE_PATH_INVALID` | `<parent>/ffs-cloud-launch-claude` exists and is not a dispatcher-marked directory (a hand-made clone, a clone from an earlier release, a symlink or a plain file), or the path overlaps your checkout. An optional reason such as `MARKER_UNRECOGNIZED`, `MARKER_EXISTS` or `MOVED_AWAY` may follow. FFS never deletes it. Remove it yourself. |

Two outcomes are not failures. When the client is still running at the launch
timeout, or exits with no session id, URL or known error, the task is
`launched` with `launch_note` `no-session-url` or `exit-no-session-url`. The
orchestrator keeps polling `claude/*` for a result that carries the task's
nonce until the deadline. It needs no `adopt`.

Doctor findings you are likely to see on a first run:

| Code | Level | Fix |
|---|---|---|
| `CLOUD_FILES` | FAIL | Commit and push `.ffs/cloud/*` (step 2). |
| `BASE_PUSHED` | FAIL | Push your current commit. |
| `BASE_CLEAN` | FAIL | Commit or stash tracked changes. |
| `API_KEY_BACKEND` | FAIL | Unset `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. |
| `NO_PROVIDER` | FAIL | Install and log in to a provider CLI. For Codex, also set the environment id variable. |
| `SANDBOX` | FAIL | `sandbox-exec` is missing or its self-check failed. Cloud stays off on this machine. |
| `RULESET` | FAIL or WARN | Add a GitHub ruleset on `ffs/cloud/**`. FAIL when a secret name is exposed. |
| `HISTORY_SECRET` | FAIL | A secret-shaped finding, binary blob or secret-named file is in your base history. See [Doctor and history findings](cloud-delegation.md#doctor-and-history-findings). |
| `VERIFY_CMD` | WARN | Set `verify.cmd` in the config. |
| `SESSION_HOOK` | WARN | Run `env init --register-hook`, then commit `.claude/settings.json`. |
| `LOGIN_UNVERIFIED` | WARN | FFS could not check the login. Confirm it yourself. |

If the orchestrator dies, `status` shows `alive=false`. Restart it from a shell
that has your exports:

```bash
bash scripts/gsd/cloud-dispatch.sh orchestrate --run <run> --resume --detach
```

For stuck launches, ambiguous candidates, failed tests and a secret leak, see
the [recovery runbooks](cloud-delegation.md#recovery-runbooks).

## What was verified live

Claude Code 2.1.329, 2026-11-20, one trivial docs task, driven through
`cloud-dispatch.sh` directly:

- The launch on a pseudo-terminal took about 8 seconds once trust was accepted.
  FFS parsed the session id and URL from the client's output.
- The cloud worker pushed its result branch about 2.5 minutes after launch.
- Killing the orchestrator while it polled, then one `orchestrate --resume`,
  produced no second launch.
- A first attempt failed because the worker wrote `tests_run` items as strings.
  Each item must be an object `{"cmd": "<command>", "rc": <exit code>}`. The
  worker packet now says so.
- A second run passed every gate (binding, isolation, path and secret checks,
  review by the opposite model family, sandboxed tests) and reached
  `ffs/cloud/<run>/integration` in under 3 minutes.

Not verified live: the Codex backend, whether `--model` and `--effort` take
effect, more than one task or dependencies between tasks, the installed
`/swarm --cloud` skill path, the secrets modes, restart driven by `reconcile`,
and a client stuck on a screen other than the trust dialog. See
[Live verification](cloud-delegation.md#live-verification).
