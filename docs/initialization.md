# Initialization — what /ffs-init sets up, and what happens if you skip it

FFS initialization has three markers. All three exist after a full onboarding;
each is independently checkable, and nothing in FFS hard-fails when one is
missing — gates degrade to advisory instead.

| Marker | Created by | Checked by |
| --- | --- | --- |
| Skills install manifest (`.feature-fix-swarm/install-manifest.json` project-scope, or the user-scope copy under `~/.cache/feature-fix-swarm/`) | `bash setup.sh --scope user\|project` | `setup.sh --doctor`, `init-guard.sh` |
| Dependencies (the roster below) | `bash scripts/gsd/deps.sh install` for the repo-scoped rows; operator-run commands for system tools | `deps.sh check`, `init-guard.sh` |
| Environment registry (`config/environments.yaml`, committed) | `/ffs-init` registry flow via `env-registry.sh apply` | `env-registry.sh check`, `gates.py`, `init-guard.sh` |

## The dependency roster (`scripts/gsd/deps.sh`)

The roster is declared once, in `deps.sh` itself, as rows of
`name | kind | required/optional | remedy`. Kinds:

- **binary** — probed with `command -v`; comma-separated names mean any-of
  (`shasum,sha256sum`, `claude,codex`, `timeout,gtimeout`).
- **npm** — `@opengsd/gsd-core` at the exact pinned version, resolved from
  `node_modules/` (the same comparison `setup.sh` makes before installing).
- **pip** — `filelock`, verified by the same symbol-presence floor probe
  `scripts/coord/coord.py` uses (`SoftFileLease`), never a version-string
  parse.
- **pin** — the staged external skills (`prompt-master`, `socratic`),
  installed by `setup.sh`.

Verbs:

```bash
bash scripts/gsd/deps.sh check            # probe everything; exit 1 if a REQUIRED row is missing
bash scripts/gsd/deps.sh check --json     # same, as a JSON array
bash scripts/gsd/deps.sh install          # repo-scoped installs only (npm ci + pip -r requirements-dev.txt)
bash scripts/gsd/deps.sh install --yes    # skip the npm-ci confirmation
bash scripts/gsd/deps.sh install --optional  # also install the contributor set (pytest, bandit, pytest-cov)
```

Exit codes: `0` ok · `1` required missing / usage · `2` install failure.

What `install` deliberately does **not** do: run brew, apt, or any system
package manager; use sudo; install anything global. Missing system tools
(`gh`, `jq`, `bats`, `shellcheck`, `tmux`, `canary`, `playwright`, …) are
reported with the exact install command and left to the operator. `npm ci`
only runs when `@opengsd/gsd-core` is absent or off-version — a working
`node_modules/` is never wiped; the pip install uses the sanctioned
`python3 -m pip install --requirement requirements-dev.txt` form (see
[Dependencies and integrations](dependencies.md)).

The roster is mechanically tested against the docs:
`tests/test_docs_dependency_roster.py` fails when a required roster row is
missing from `docs/dependencies.md`.

## /ffs-init as the umbrella

`/ffs-init` runs Phase 0 (deps) before its registry flow: `deps.sh check` →
`deps.sh install` when the repo-scoped rows are missing → an explicit-confirm
offer of `setup.sh` when no install manifest exists → a report of missing
system tools → then the environment-registry interview documented in
`skills/ffs-init/SKILL.md` (and in the environment-registry guide once PR #105
lands). `--skip-deps` skips Phase 0;
`--detect-only` and `--check` never run it (those modes stay read-only and
hermetic).

## The pre-init guard (`scripts/gsd/init-guard.sh`)

Every operator entrypoint skill (`feature-implement`, `feature-spec`,
`task-swarm`, `fix`, `swarm`, `code-uplift`, `preflight`) carries an
`## Init gate` section (lint-enforced by `tests/test_host_dispatch_lint.py`):
before doing anything else it runs

```bash
bash "$(git rev-parse --show-toplevel)/scripts/gsd/init-guard.sh" || true
```

and relays the output. In an interactive session the skill offers to run
`/ffs-init` first; declining proceeds anyway. Headless, spawned, and
autonomous runs relay the warnings once and continue.

The guard is **advisory by design** — it always exits 0, following the
installer's "one hint line only — never a gate" precedent, so it can never
strand an unattended run. `--strict` exits 1 for callers that want a hard
gate; nothing in FFS enables strict mode.

Two semantics worth knowing:

- The registry marker follows `gates.py`'s authority: the file must be
  **tracked in HEAD**. An uncommitted `config/environments.yaml` governs
  nothing, and the guard borrows `gates.py`'s own advisory wording (including
  the "activate it with git add && git commit" remedy) rather than forking it.
- The dependency marker delegates to `deps.sh check` — the guard never
  re-implements the roster.

## Consumer repositories

`setup.sh --reconcile-consumer <dir>` copies every `scripts/gsd/*.sh` into a
consumer repo (honoring `scripts/gsd/fork-allowlist.txt`), so `deps.sh` and
`init-guard.sh` are available there too. The npm/pip rows describe the FFS
checkout itself; in a consumer repo their absence simply means FFS's own
checkout is where the install happens.
