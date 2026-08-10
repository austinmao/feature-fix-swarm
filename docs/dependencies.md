# Dependencies and integrations

Feature Fix Swarm is intentionally an integration layer. This page separates
what FFS owns from what it installs, invokes, or merely integrates with.

## Required runtime

| Component | Version policy | Ownership | Purpose |
| --- | --- | --- | --- |
| Claude Code or Codex CLI | At least one current supported host; Codex `>=0.137.0,<0.148.0` | User-installed | Runs the skills and agents |
| Open GSD Core | Exact `@opengsd/gsd-core@1.10.0` | Upstream-owned; installed through GSD's installer | Plan/execute/verify orchestration, manifests, hooks, and GSD skills |
| Node.js and npm | Node 22+, npm 10+ | User-installed | Reproducible GSD package installation |
| Python | 3.11+ | User-installed | Installer, gates, state, and verification tools |
| Git | Current supported release | User-installed | Source control, common-directory locks, and worktrees |
| Bash or zsh | macOS/Ubuntu system shell | User-installed | Runtime and hook scripts |

`package-lock.json` is committed and `npm ci` is the supported dependency
install. The GSD version is repeated in package metadata and runtime checks so
an accidental floating upgrade fails before installation or launch.

Contributor-only Python tools are exact-pinned in `requirements-dev.txt`.
They do not ship into a consumer repository.

`bash scripts/gsd/deps.sh check` probes every dependency on this page (and is
the roster the docs are tested against — `tests/test_docs_dependency_roster.py`);
`deps.sh install` performs the repo-scoped installs (`npm ci`,
`pip install --requirement requirements-dev.txt`) and never touches system
packages. `/ffs-init` runs both as its Phase 0.

## Required command-line tools

| Component | Version policy | Ownership | Purpose |
| --- | --- | --- | --- |
| [GitHub CLI (`gh`)](https://cli.github.com) | Current release, authenticated | User-installed | Ship/finalize tail: `assert-merged.sh`, run watching, PR operations — no fallback |
| [`jq`](https://jqlang.github.io/jq/) | Current release | User-installed | Canary gate and QA-coverage adversary JSON handling |
| `shasum` or `sha256sum` | Either | System (perl / coreutils) | Evidence and cache-key hashing across the run scripts |
| `ps` | POSIX | System | Host process detection in `adversary-host.sh` |

Optional tools that degrade fail-soft when absent: `curl` (browser-proof
readiness probe), `flock` (locking; shell fallback), `timeout`/`gtimeout`
(bounded runs; python3 fallback), `tmux` (worktree-GC session check),
`npx`/vitest and `playwright` and `canary` (browser/JS QA lane).

## Contributor and CI tooling

| Component | Version policy | Ownership | Purpose |
| --- | --- | --- | --- |
| [bats-core](https://github.com/bats-core/bats-core) | Contributor-installed | User-installed | Runs `tests/bats/*.bats` — plan-wall, fence, adversary-equivalents, probe-lib |
| [ShellCheck](https://github.com/koalaman/shellcheck) | Contributor-installed, `-S warning` at repo convention | User-installed | Lints every changed/new shell script; matches `CONTRIBUTING.md` and CI |
| [pytest](https://pytest.org) + pytest-cov | Exact pins in `requirements-dev.txt` | PyPI via `deps.sh install --optional` | Python test suites under `lib/tests/` and `tests/` |
| [Bandit](https://github.com/PyCQA/bandit) | Exact pin in `requirements-dev.txt` | PyPI via `deps.sh install --optional` | Scans production Python in CI (`bandit -r lib scripts -lll`) |
| `slopcheck` | Optional | Not installed by FFS | Package-legitimacy verdict (`OK\|SUS\|SLOP`) consulted by the `feature-implement` install gate; absent binary degrades to `[ASSUMED]`, never a hard failure |

## Coordination layer

| Component | Version policy | Ownership | Purpose |
| --- | --- | --- | --- |
| [filelock](https://github.com/tox-dev/filelock) | `>=3.30,<4` (floor-plus-ceiling, not exact-pinned) | Third-party, PyPI-installed via `requirements-dev.txt` — never vendored | Cross-process/cross-session registry mutex (`filelock.FileLock`) and the process-liveness primitives (`filelock._identity.process_alive`/`process_start_token`/`host_name`) `scripts/coord/coord.py` uses to decide whether a claim's holder is provably dead |

`filelock` is MIT-licensed and published by the `tox-dev` GitHub organization.
The `>=3.30,<4` range deliberately departs from this file's exact-pin
convention used elsewhere: `filelock` is the first third-party **runtime**
(not contributor-only) Python dependency this file describes, and its
version is *probed at runtime* by `coord.py doctor` — via `SoftFileLease`
(added 3.30.0) and `ReadWriteLock` (added 3.21.0) symbol presence, not a
version-string parse — rather than trusted as a hard pin. The `<4` ceiling
guards the one private symbol `coord.py` imports
(`filelock._identity.process_start_token`), which a major bump could remove
without a deprecation cycle. Install via `python -m pip install --requirement
requirements-dev.txt`; a bare `pip install filelock` is not sanctioned
because the resolver can silently land on a pre-3.30 release that lacks the
floor-verification symbols.

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

## Pinned external sources

### prompt-master

`prompt-master` is installed from
`nidhinjs/prompt-master@d15eabbe5d2122eedc060bae8a771381e9873d1b`.
FFS applies the small patch in `vendor/prompt-master/codex-gpt56.patch` and
records both the upstream commit and patch hash in the installed copy. The
patch has been submitted upstream as
[prompt-master #57](https://github.com/nidhinjs/prompt-master/pull/57).

This pin avoids executing a moving branch during setup. `setup.sh` preserves
an existing unowned destination rather than overwriting it.

### socratic

`socratic` is installed from
`m4vic/socratic@8c7e1fdda5ff6f7755d4855907ddf0022a755493` via
`scripts/install-socratic.sh`, staged by `stage_socratic()` into
`.agents/skills/socratic` (with a `.claude/` project-scope symlink) exactly
like `prompt-master`. Installation is default and skippable with
`FFS_SKIP_SOCRATIC=1`; the emission-side kill switch is `SOCRATIC=off` and
the vendor-tree override is `FFS_SOCRATIC_DIR`.

Two more env vars steer `stage_socratic()` at the subprocess boundary, both
read by `lib/ffs_installer.py`: `FFS_SOCRATIC_INSTALLER` overrides the
installer path itself (a test seam — points install-time tests at
`tests/fixtures/socratic-installer-stub.sh` instead of the real
`scripts/install-socratic.sh`, mirroring the pre-existing
`FFS_GSD_INSTALLER` precedent), and `FFS_SOCRATIC_SOURCE` overrides the
clone source, appended to the installer command as `--source <value>` when
set (the same seam `test_install_socratic_honours_source_override`
exercises directly against the real installer). Neither is meant to be set
in normal operation.

Both pinned external skills now share one staging implementation
(`_stage_external_skill()` in `lib/ffs_installer.py`) and therefore one
uniform env contract: `FFS_SKIP_<SKILL>`, `FFS_<SKILL>_INSTALLER`,
`FFS_<SKILL>_SOURCE` for `PROMPT_MASTER` and `SOCRATIC` alike. The
`*_INSTALLER` override stays an env var by design rather than a
monkeypatch seam: install-time tests drive `setup.sh` as a subprocess, and
only the environment crosses that boundary.

No patch is pinned at this commit. `vendor/socratic/pin.json` carries no
`patch` key at all, and the installed marker (`.ffs-socratic.json`, schema
`ffs.external-skill/v1`) accordingly records a null `patch_sha256` — this is
a fact about this pin, not a placeholder. The `patch` key is optional:
`install-socratic.sh` reads it through a `dict.get("patch")`, so an absent
key and an explicit JSON `null` take the identical code path. When a patch
is named it must be a bare filename — any value containing a slash is
refused with exit 2 before resolution — and it is resolved inside
`vendor/socratic/`, so a patch can never be pulled from outside the vendor
directory.

The upstream-submission channel is the same convention `prompt-master`
already exercises: a compatibility patch is submitted upstream and the
resulting PR URL is recorded in `pin.json`'s `upstream_submission`. That
field is `null` here because no patch has been needed at this commit.

### Bumping the socratic pin

1. Audit the upstream diff between the current pin and the candidate commit,
   paying attention to question files, pack directories, and `SKILL.md` —
   those are what actually reach a reviewer prompt.
2. Update `commit` in `vendor/socratic/pin.json`, adding a `patch` bare
   filename beside it only if an FFS compatibility patch is genuinely
   required.
3. Reinstall: `bash scripts/install-socratic.sh --dest <path>` for a
   standalone check, or a full `setup.sh` run to exercise `stage_socratic()`'s
   fingerprint tracking and the manifest entries `uninstall` and `doctor`
   read.
4. Run the socratic suites: `bats tests/bats/socratic-slice.bats
   tests/bats/socratic-spec-step.bats tests/bats/socratic-plan-wall.bats
   tests/bats/socratic-review-gate.bats` plus `python3 -m pytest
   tests/test_installer.py -q`.
5. Amend the enum tables in `scripts/gsd/socratic-slice.sh` if the bump
   added, renamed, or removed a question file or a pack directory.
   `DOMAIN_ENUM_ORDER` and `PACK_ENUM` are a frozen mirror of the upstream
   tree at this exact pin, hosted once and shared by both validation
   postures. A layout change degrades silently in two opposite directions at
   once: the emission path is fail-soft, so a domain whose file vanished is
   skipped with a `WARN` and a thinner slice still arms; `--validate` is
   fail-closed, so a spec author declaring a domain that upstream genuinely
   added is rejected at exit 3 with a spec that is correct. Neither is a
   test failure — every bats suite runs against `make_vendor_tree` fixtures
   and never against the real pin — so CI cannot catch enum drift. Besides
   this step, the opt-in suite `tests/bats/socratic-enum-drift.bats` asserts
   the 1:1 enum↔file mapping against a REAL resolved vendor tree (it skips
   wherever no tree resolves, i.e. on CI) — run it on any machine with the
   new pin installed to catch drift mechanically.

## Optional integrations

### Opposite-host review

Installing both Claude Code and Codex enables cross-vendor adversarial review.
With only one host, FFS can use a different model on the active vendor but
marks the provenance degraded. A degraded fallback cannot satisfy a gate that
requires an exact model or vendor.

### gbrain

[gbrain](https://github.com/garrytan/gbrain) is an optional workspace-memory
CLI. `learnings-harvest.sh` writes to it when the binary is present and
healthy; absent, learnings archive locally instead. Every gbrain call site is
fail-soft — see `docs/gbrain-optional.md`.

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
- CodeQL, Bandit, ShellCheck, and OpenSSF Scorecard cover different parts of
  the release surface; no single scanner is treated as a complete security
  proof. (`npm audit` is a PR-checklist item, not a CI job; no secret-scanning
  workflow is wired today — the credential guards under `scripts/` are the
  in-repo line of defense.)
- Network-fetched dependencies are never described as trusted merely because
  they are popular. Pins, manifests, hashes, and reviewable diffs establish
  the boundary.

See [SECURITY.md](../SECURITY.md) for private vulnerability reporting and
[Installer, migration, and rollback](installer.md) for filesystem ownership.
