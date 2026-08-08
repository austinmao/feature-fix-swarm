# Installer, migration, and rollback

FFS has two explicit installation scopes. The installer owns FFS skills and
the pinned `prompt-master` compatibility copy. Setup invokes the exact pinned
GSD upstream installer for complete Claude and Codex profiles; that upstream
installer remains the only writer and owner of `gsd-*` skills, agents, hooks,
and configuration.

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

For one transition release, `bash setup.sh` still means `--scope user` and
prints a deprecation warning. Scripts and documentation should use the
explicit form now.

Resolution is project first, then user. Having the same release in both
scopes is a degraded doctor warning but remains compatible. Different release
versions are an actionable incompatibility.

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

Doctor also requires upstream `gsd-file-manifest.json` ownership at GSD 1.10.0
with full profiles in both the Claude and Codex config roots. If Codex CLI is
installed, its supported range is `>=0.137.0,<0.148.0`; `0.146.x` and `0.147.x`
are the tested lines.

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
before canonical FFS paths are changed.

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

`setup.sh` verifies the exact `@opengsd/gsd-core@1.10.0` package and runs:

```text
gsd-core --claude --global --profile=full
gsd-core --codex --global --profile=full
```

FFS orchestrates these calls but never copies or rewrites GSD source
artifacts. Upstream manifests remain the source of ownership truth.
