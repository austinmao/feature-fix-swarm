# Installer, migration, and rollback

FFS has two explicit installation scopes. The installer owns FFS skills and
the pinned `prompt-master` compatibility copy. Setup first applies and
verifies its byte-pinned GSD compatibility overlay on a staged copy of the
package (the repository's `node_modules` stays pristine), then invokes the
exact pinned GSD upstream installer for complete Claude and Codex profiles.
That upstream installer remains the only writer and owner of `gsd-*` skills,
agents, hooks, and configuration.

## Project scope

Use project scope for repositories that should carry a reproducible FFS
installation:

```bash
bash setup.sh --scope project --project-dir /absolute/path/to/repo
```

The installer vendors the FFS skill sources under
`.feature-fix-swarm/vendor/skills/`. Both `.agents/skills/` and
`.claude/skills/` contain relative links to those sources, so the checkout can
move between machines without rewriting links. The patched external
`prompt-master` copy is canonical under `.agents/skills/prompt-master`, with a
relative Claude link. Nothing is installed under `.codex/skills`.

Project operations lock through `git rev-parse --git-common-dir`. A primary
checkout and all of its linked worktrees therefore share one installer lock.

The scope flag controls FFS discovery only. Both project and user setup also
ensure the pinned, upstream-owned GSD full profiles in the user-global Claude
and Codex roots shown under **GSD ownership** below.

## User scope

Use user scope to make FFS available outside a particular repository:

```bash
bash setup.sh --scope user
```

User installs are hash-managed copies in both `~/.agents/skills` and
`~/.claude/skills`. Their manifest is
`~/.cache/feature-fix-swarm/install-manifest.json`.

User installs also stage the managed lib runtime at
`~/.claude/lib/feature-fix-swarm/`: the Python modules the shipped scripts
import (`gates.py`, `clean_code_metrics.py`, `runtime_proof.py`, `model_requests.py`,
`host_capabilities.py`, `process_identity.py`, `run_context.py`,
`operator_guard_hook.py`, the
`run_state/` package and the cloud-delegation modules), a few scripts
(`scripts/gsd/socratic-slice.sh`, `fence-data.sh`, `cloud-dispatch.sh`,
`cloud-review.sh`, `adversary-host.sh`, `run-bounded.sh`, `review-tier.sh`,
`security-surface.sh` and `codex-runtime-observer.py`), `operator-only-guard.sh`, the supervised-dispatch
patch and `cloud-templates/`. `MANAGED_LIB_FILES` in `lib/ffs_installer.py` is
the exact list. These are the paths the shipped scripts resolve when no
repo-local copy shadows them. They are manifest-tracked like skills, so
`--doctor` flags a drifted/stale copy and `--uninstall` removes them.
`--reconcile-consumer` copies every regular `scripts/gsd/*.sh` and `*.py` into the
consumer, plus three hook scripts (`cli-hang-guard.sh`,
`credential-output-guard.sh`, `operator-only-guard.sh`), the `lib/` modules listed in
`reconcile_consumer`, `lib/cloud-templates/` and `schemas/review-finding.schema.json`, so vendored
consumers stop running stale gate logic. A file named in the consumer's
`scripts/gsd/fork-allowlist.txt` (first token of each non-comment line) is
skipped when it already exists there. To check a consumer afterwards, see
[`sync-drift-check.sh`](#sync-drift-checksh).
review-gate runs these helpers from a copy of the tip of `origin/main` (else `main`), never
from the reviewed working tree, so commit the reconciled runtime to `main`.
`origin` must be the repository the change merges into, and its `main` must be protected.
When the base ref has no FFS runtime it uses the user-scope copies of
`adversary-host.sh`, `run-bounded.sh`, `review-tier.sh` and `security-surface.sh`
under `~/.claude/lib/feature-fix-swarm/scripts/gsd/`.

A pre-existing hand-copied file at the managed path (the old workaround for
this gap) is an unmanaged collision: the install blocks and preserves it
unless you pass `--adopt-collisions`, which backs it up and stages the fresh
bytes. Project-scope installs do not stage the home-path runtime — a machine
using only project scope should also run `--scope user` once so the ladder's
`~/.claude` rung resolves; `--reconcile-consumer` copies only into the consumer.

For one transition release, `bash setup.sh` still means `--scope user` and
prints a deprecation warning. Scripts and documentation should use the
explicit form now.

## Options and environment

`bash setup.sh` runs `lib/ffs_installer.py`. With no action flag it installs.

| Option | Meaning |
| --- | --- |
| `--scope project\|user` | Installation scope. |
| `--project-dir PATH` | Repository for project scope. |
| `--doctor`, `--json` | Check an installation; `--json` prints the `ffs.doctor/v1` report. |
| `--rollback ID` | Restore a backup. Accepts only a backup id. |
| `--uninstall` | Remove manifest-owned paths. |
| `--reconcile-consumer PATH` | Copy the packaged levers into a vendoring consumer repo. |
| `--adopt-collisions` | Back up and replace colliding paths instead of blocking. Install only. |
| `--yes`, `-y` | Compatibility no-op. Installs are non-interactive and the installer never reads this flag. |

Choose at most one of `--doctor`, `--rollback`, `--uninstall` and
`--reconcile-consumer`. An invalid invocation exits 2 and prints
`ERROR: <reason>` on stderr.

`FFS_VERSION` overrides the release version the installer stamps into the
manifest. When unset, the version is the first `## v<version>` heading in
`CHANGELOG.md`, and `0.0.0-dev` if there is none
(`source_version` in `lib/ffs_installer.py`). Doctor compares these versions
across project and user scope.

## Operator-only guard hook

Both scopes register `scripts/hooks/operator-only-guard.sh` as a PreToolUse
`Bash` hook. It blocks agent tool calls that run these operator-only
commands:

- `findings-queue resolve --disposition issue|descope`
- `findings-queue retag --in-spec`
- `intent-check --repin`
- `claude` with `--cloud` or `--cloud=<value>`
- `codex cloud exec`, `codex cloud apply` and `codex cloud diff`
- `cloud-dispatch.sh` (or `cloud_dispatch.py`) with `adopt`, `abandon` or
  `resolve`

The operator runs them in their own shell, where hooks do not fire. A denied
call exits 2 and prints `operator-only-guard: <gate> is operator-only` on
stderr. The three `findings-queue` / `intent-check` gates match by token
membership, so a segment that only contains those words (an `echo`, heredoc
text) is also denied. The three cloud gates match by executable position
(first command of a shell segment, after `VAR=value` assignments, wrappers
such as `env`, `sudo` and `rtk`, and `bash -c` or `python3` launchers),
compared by base name, so `git grep -e codex -e cloud -e diff` is allowed.
There is no kill-switch.

The hook is an anti-accident control, not a security boundary. A script file
or a Python import gets past it. It fails open: empty input, a missing
`python3`, or any internal exception (malformed JSON included) exits 0 and
allows the call. A shell quoting error is not one of those cases; the guard
falls back to scanning the raw text. The registered command runs the script only if the file
exists, so a removed or half-uninstalled copy is a silent no-op, and the hook
has a 10 second timeout. See
[Guard patterns](cloud-delegation.md#guard-patterns) for the cloud gates.

- User scope: the entry goes into `~/.claude/settings.json` (command runs the
  managed-lib copy) and `~/.codex/hooks.json` (command runs a copy in
  `~/.codex/hooks/`). Headless Claude and Codex staging register it too. When
  `~/.claude` is a symlink, the Claude stage runs the live-home copy rather than
  the staged one.
- Project scope: the script is vendored to
  `.feature-fix-swarm/vendor/hooks/`, and the entry goes into the project's
  `.claude/settings.json` and `.codex/hooks.json`. Codex loads project hooks
  only for trusted projects.
- Install adds exactly one FFS-owned entry, leaves every other entry and key
  alone, and records it in the manifest under `hooks`. Uninstall removes only
  that entry. A symlinked, unparseable, empty or non-object hook file is
  skipped with a warning and the rest of the install continues.

Resolution is project first, then user. Having the same release in both
scopes is a degraded doctor warning but remains compatible. Different release
versions are an actionable incompatibility.

## Cloud delegation SessionStart hook

`setup.sh` does not register a `SessionStart` hook. It only ships the pieces
the opt-in `/swarm --cloud` path needs: the cloud runtime modules,
`operator_guard_hook.py` and `cloud-templates/` (see
[Cloud delegation: Installer](cloud-delegation.md#installer)). The operator
registers the hook per repository, from the main worktree:

```bash
bash scripts/gsd/cloud-dispatch.sh env init --register-hook
```

This writes `.ffs/cloud/config.json`, `setup.sh`, `env-check.sh` and
`session-start.sh` (existing files are kept unless you pass `--force`), and
merges one `SessionStart` entry into the repository's `.claude/settings.json`:
`bash "$CLAUDE_PROJECT_DIR/.ffs/cloud/session-start.sh"` with a 300 second
timeout. The merge keeps every other entry and key. Nothing is committed.

The installer manifest records only the guard entry under `hooks`, so
`--uninstall` does not remove this one. Delete it from `.claude/settings.json`
by hand if you stop using cloud delegation. The full procedure is in
[Cloud delegation](cloud-delegation.md#quick-start).

## Doctor

Check either scope after installation:

```bash
bash setup.sh --doctor --scope user
bash setup.sh --doctor --scope project --project-dir /absolute/path/to/repo
bash setup.sh --doctor --scope project --project-dir /absolute/path/to/repo --json
```

JSON output has schema `ffs.doctor/v1`. Exit codes are stable:

- `0`: compatible; the report may contain a degraded same-version warning.
- `1`: actionable incompatibility, such as drifted hashes, an edited legacy
  collision, or different project/user versions.
- `2`: invalid invocation or an internal installer failure.

Doctor also requires upstream `gsd-file-manifest.json` ownership at GSD 1.15.0
with full profiles in both the Claude and Codex config roots. If Codex CLI is
installed, its supported range is `>=0.137.0,<0.148.0` or exact `0.154.0` / `0.155.1` / `0.156.1` / `0.157.0` / `0.158.0` / `0.159.0` / `0.160.0` / `0.162.1`;
`0.146.x`, `0.147.x`, `0.154.0`, `0.155.1`, `0.156.1`, `0.158.0`, `0.159.0`, `0.160.0`, and `0.162.1` are the tested lines; `0.157.0` is admitted without a live
probe, pending qualification. Intervening and other later
releases remain refused until individually qualified.

## Safe migration

The historical catalog at
`data/installer/legacy-skill-hashes.json` contains the shipped skill hashes
from v4.13.0 through v4.22.0. During an upgrade the installer removes legacy
`.codex/skills` files only when their hashes occur in that catalog (or match
the release currently being installed). Broken legacy links can also be
removed safely. Edited or unknown files are preserved, named on stderr, and
leave the operation at exit `1` until the operator resolves them.

The same rule applies to managed destinations: an existing path is replaced
only when it matches the prior install manifest, already matches the new
source, or is a broken project link. Unmanaged and edited collisions fail
before canonical FFS paths are changed — by default, a single colliding path
hard-blocks the entire install, even for skills that never changed.

Pass `--adopt-collisions` to resolve collisions instead of blocking: each
colliding path is backed up (its location is printed as
`adopted collision: <path> backed up at <backup path>`), then replaced with
the vendor copy, while the rest of the install proceeds normally. The default
behavior is unchanged unless this flag is passed.

## Backups, rollback, and uninstall

Every install and uninstall emits a `backup_id`. The backup lives at
`~/.cache/feature-fix-swarm/backups/<id>/` and includes a `0600` manifest plus
the prior bytes and links for every replaced path. Before invoking GSD, setup
snapshots the paths declared by the prior upstream manifests and the shared
configuration files GSD may merge. Rolling back an upgrade therefore restores
the prior GSD version markers/manifests and discovery layout as well as FFS.

```bash
bash setup.sh --rollback 20260801T120000Z-12345-a1b2c3
```

Rollback uses compare-before-restore semantics. If a path changed after the
recorded operation, it is preserved and rollback exits `1`; concurrently
edited data is never overwritten. Rollback does not inspect or touch
`.planning`.

Removal is deliberately explicit:

```bash
bash setup.sh --uninstall --scope user
bash setup.sh --uninstall --scope project --project-dir /absolute/path/to/repo
```

Only manifest-owned paths whose hashes still match are removed. Edited copies
are preserved and reported. The uninstall itself is backed up and can be
rolled back with the emitted ID.

## GSD ownership

`setup.sh` verifies the exact `@opengsd/gsd-core@1.15.0` package and runs:

```text
gsd-core --claude --global --profile=full
gsd-core --codex --global --profile=full
```

FFS orchestrates these calls and never copies GSD runtime artifacts into its
own catalog. The sole exception is the audited, deterministic RED-evidence
overlay described in [Dependencies](dependencies.md#why-gsd-is-a-dependency-instead-of-vendored-code):
setup applies and verifies it against both the pristine and rendered digests
before dispatch. Upstream manifests remain the source of ownership truth for
global profiles.

## sync-drift-check.sh

A consumer repo that vendors the levers (for example through
`--reconcile-consumer`) carries live copies that can drift. Run
`scripts/gsd/sync-drift-check.sh` from the consumer side after every vendor
sync, or from consumer CI. It compares each `*.sh` and `*.py` file in the
packaged `scripts/gsd` directory with the same filename in the consumer
directory.

```bash
bash scripts/gsd/sync-drift-check.sh <consumer-scripts-dir> [--allowlist FILE]
```

The allowlist has one line per deliberate fork: `<filename> <free-text reason>`.
`GSD_SYNC_SRC` overrides the packaged directory (default: the directory that
holds the script itself).

| Line | Meaning |
| --- | --- |
| `IN-SYNC: <file>` | Identical. |
| `MISSING: <file>` | No consumer copy. Warning only; partial installs are legal. |
| `FORKED: <file> (<reason>)` | Differs and is allowlisted. |
| `DRIFT: <file> ...` | Differs and is not allowlisted. Counts toward exit 1. |
| `STALE-ALLOWLIST: <file> ...` | Allowlisted but identical. Prune the entry. |

Exit codes: `0` when no unlisted drift exists (the last line is
`[sync-drift-check] OK`); `1` when any file shows `DRIFT`; `2` when the
consumer directory is missing, or when the source and consumer are the same
directory (a self-compare would report every file as `IN-SYNC`).
