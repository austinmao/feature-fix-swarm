# Dependencies and integrations

Feature Fix Swarm is intentionally an integration layer. This page separates
what FFS owns from what it installs, invokes, or merely integrates with.

## Required runtime

| Component | Version policy | Ownership | Purpose |
| --- | --- | --- | --- |
| Claude Code or Codex CLI | At least one current supported host; Codex `>=0.137.0,<0.147.0` | User-installed | Runs the skills and agents |
| Open GSD Core | Exact `@opengsd/gsd-core@1.9.1` | Upstream-owned; installed through GSD's installer | Plan/execute/verify orchestration, manifests, hooks, and GSD skills |
| Node.js and npm | Node 22+, npm 10+ | User-installed | Reproducible GSD package installation |
| Python | 3.11+ | User-installed | Installer, gates, state, and verification tools |
| Git | Current supported release | User-installed | Source control, common-directory locks, and worktrees |
| Bash or zsh | macOS/Ubuntu system shell | User-installed | Runtime and hook scripts |

`package-lock.json` is committed and `npm ci` is the supported dependency
install. The GSD version is repeated in package metadata and runtime checks so
an accidental floating upgrade fails before installation or launch.

Contributor-only Python tools are exact-pinned in `requirements-dev.txt`.
They do not ship into a consumer repository.

## Why GSD is a dependency instead of vendored code

GSD already owns the durable planning model and the plan/execute/verify loop.
Copying its skills or hooks into FFS source would create two implementations,
make security fixes hard to trace, and blur who is responsible for migrations.

FFS therefore calls GSD's full-profile installer and verifies its upstream
manifest. FFS owns only the surrounding integration: cross-host discovery,
safe migration and rollback, run locking, phase evidence, typed model routing,
adversarial review, sandbox/auth handling, and operator grants.

When GSD is upgraded, update the exact package pin, lockfile, installer
constant, compatibility matrix, and relevant fixtures together. Never copy or
patch installed GSD artifacts by hand.

## Pinned external source

`prompt-master` is installed from
`nidhinjs/prompt-master@d15eabbe5d2122eedc060bae8a771381e9873d1b`.
FFS applies the small patch in `vendor/prompt-master/codex-gpt56.patch` and
records both the upstream commit and patch hash in the installed copy. The
patch has been submitted upstream as
[prompt-master #57](https://github.com/nidhinjs/prompt-master/pull/57).

This pin avoids executing a moving branch during setup. `setup.sh` preserves
an existing unowned destination rather than overwriting it.

## Optional integrations

### Opposite-host review

Installing both Claude Code and Codex enables cross-vendor adversarial review.
With only one host, FFS can use a different model on the active vendor but
marks the provenance degraded. A degraded fallback cannot satisfy a gate that
requires an exact model or vendor.

### gstack

[gstack](https://github.com/garrytan/gstack) supplies optional skills used by
the broader workflow, including idea shaping, browser QA, investigation,
review, and shipping. FFS does not install or update gstack. Workflows that
name a gstack skill require that skill to be available in the active host.

### GitHub Spec Kit

[Spec Kit](https://github.com/github/spec-kit) supplies the specification and
planning bootstrap used by the full `feature-spec` path. It is not needed for
an existing GSD plan, `fix`, or the lightweight `task-swarm` path.

### Agent catalogs

FFS discovers repository-local Claude and Codex agents and accepts an explicit
local agent seed. External catalogs such as ECC or wshobson/agents are optional
sources of specialized roles; they are not installed or refreshed by FFS. A
generic built-in role floor keeps basic decomposition usable without them.

## Update and security policy

- Direct dependencies remain exact-pinned and lockfile-resolved.
- GitHub Actions are pinned to full commit SHAs with release comments.
- Dependabot watches npm and GitHub Actions weekly.
- `npm audit`, CodeQL, Gitleaks, Bandit, ShellCheck, and OpenSSF Scorecard cover
  different parts of the release surface; no single scanner is treated as a
  complete security proof.
- Network-fetched dependencies are never described as trusted merely because
  they are popular. Pins, manifests, hashes, and reviewable diffs establish
  the boundary.

See [SECURITY.md](../SECURITY.md) for private vulnerability reporting and
[Installer, migration, and rollback](installer.md) for filesystem ownership.
