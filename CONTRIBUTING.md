# Contributing to Feature Fix Swarm

Thanks for helping make long-running agent work safer and more predictable.
Focused bug reports, documentation fixes, tests, and small pull requests are
especially welcome.

## Before you start

- For usage questions, read [Getting started](docs/getting-started.md) and
  [Support](SUPPORT.md).
- For bugs and feature requests, use the matching GitHub issue form.
- For security vulnerabilities, do **not** open a public issue. Follow
  [SECURITY.md](SECURITY.md).
- For a large behavioral change, open an issue first so the contract and
  compatibility impact can be agreed before implementation.

By participating, you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Requirements: macOS or Ubuntu, Git, Bash and zsh, Node.js 22+/npm 10+, and
Python 3.11+.

```bash
git clone https://github.com/austinmao/feature-fix-swarm.git
cd feature-fix-swarm
npm ci
python3 -m pip install pytest
```

The canonical FFS skill sources live in `skills/`. GSD owns the installed
`gsd-*` artifacts; do not copy or rewrite GSD source files in this repository.
See [Dependencies and integrations](docs/dependencies.md) before changing an
installer or external integration.

## Make a focused change

1. Create a branch from current `main`.
2. Add a failing test or one-command reproduction before changing executable
   behavior.
3. Update the relevant docs and examples in the same pull request.
4. Keep host-neutral skill text qualified: Codex uses `$skill`; Claude uses
   `/skill`.
5. Do not commit `.planning/`, run state, logs, caches, credentials, or local
   worktrees.

FFS preserves user-edited install collisions and depends on exact hashes and
manifests. Installer changes must cover project scope, user scope, upgrades,
rollback, and linked-worktree locking.

## Verification

Run the complete local gate:

```bash
npm audit
python3 -m pytest lib/ tests/ -q
python3 scripts/verify-skill-blocks.py
python3 scripts/lint_host_dispatch.py skills/*/SKILL.md
python3 scripts/lint_model_routing.py
python3 lib/model_requests.py lint templates/model-requests.json
shellcheck -S warning setup.sh hooks/*.sh scripts/*.sh scripts/hooks/*.sh scripts/gsd/*.sh
bats --print-output-on-failure $(find . -name '*.bats' -not -path './.git/*')
```

On macOS, also keep the zsh parser contract green:

```bash
zsh -n setup.sh scripts/gsd/gsd-run.sh scripts/install-prompt-master.sh
```

Tests that require provider credentials or live model calls must remain
explicit and must never print or commit credential values.

## Pull requests

A good pull request:

- explains the user-visible problem and why the change belongs in FFS;
- links an issue when one exists;
- calls out compatibility, migration, security, and dependency effects;
- includes focused tests and the commands/results used to verify them;
- avoids unrelated generated files or cleanup; and
- does not change pins, permissions, or fail-closed behavior without a clear
  rationale.

Maintainers may ask to split broad changes so each security and compatibility
decision remains reviewable.
