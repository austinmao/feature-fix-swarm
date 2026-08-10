# CI templates and test tiers — workflows FFS proposes, never overwrites

`templates/ci/` holds five workflow templates plus a dependabot config.
`scripts/gsd/env-registry.sh render` turns them into real workflows using values
from [the environment registry](environment-registry.md). Rendering is
structurally incapable of touching a workflow you already own.

## The anti-clobber rule

`render` only ever computes `ffs-<name>.yml` targets
(`scripts/gsd/env-registry.sh:11-13`). It cannot compute the path of a workflow
it did not generate, so an existing consumer workflow is never edited, merged,
or deleted — not by policy, by construction.

| Situation | Where it lands |
|---|---|
| No collision | `.github/workflows/ffs-<name>.yml` |
| `ffs-<name>.yml` already exists | `.github/ffs-proposals/<same basename>` plus a `diff -u` for review |
| dependabot | `.github/dependabot.yml` |

Render is also all-or-nothing on validation: the registry is leak-scanned
**before** any byte of it can flow into a rendered file
(`scripts/gsd/env-registry.sh:1204-1210`), and every substituted value must
match its shape gate or nothing is written.

### Closed placeholder set

Exactly six tokens are ever substituted, and none of them carries a command:

| Token | Source | Used by |
|---|---|---|
| `{{TIER_FAST}}` | registry `test_tiers` | `pr-fast` |
| `{{TIER_FULL}}` | registry `test_tiers` | `main-full` |
| `{{TIER_NIGHTLY}}` | registry `test_tiers` | `nightly-deep` |
| `{{LOCKFILE_HASH_PATH}}` | repo lockfile | `pr-fast`, `main-full` (cache key) |
| `{{STAGING_ENV}}` | registry environments | `deploy-staging` |
| `{{PROD_ENV}}` | registry environments | `deploy-prod` |

Deploy mechanisms are deliberately **not** placeholders. Each deploy template
ships a `CONSUMER DEPLOY STEP` block that echoes and exits 0; you replace it in
your rendered copy through normal review. It exits 0 on purpose so a
placeholder can never mask the smoke gate's verdict.

## The templates

| Template | Trigger | What it does |
|---|---|---|
| `pr-fast.yml` | `pull_request` | Fast tier, 10-minute cap, concurrency with cancel-in-progress |
| `main-full.yml` | push to `main` | Full tier across a 3.11/3.12 matrix (`fail-fast: false`), uploads the tier-command transcript as an artifact |
| `nightly-deep.yml` | `schedule` + `workflow_dispatch` | Deep tier; on failure a separate job opens an issue rather than reddening the default branch |
| `deploy-staging.yml` | `workflow_call` / `workflow_dispatch` | Deploy → smoke gate → `gates.py promote` |
| `deploy-prod.yml` | `workflow_call` / `workflow_dispatch` | Default-branch refusal → `check-grant --require-environments` → deploy → smoke gate → rollback **emitted** |
| `dependabot.yml` | — | Source content for `.github/dependabot.yml` (github-actions, weekly) |

Every template carries the same hardening: SHA-pinned actions with a version
comment, workflow-level `permissions: contents: read`, `concurrency` groups,
`timeout-minutes` on every job, `fetch-depth: 1`, and
`persist-credentials: false`.

Two rules are worth stating because they are easy to undo by accident:

- **No `${{ }}` inside a `run:` block.** Caller-supplied values cross into shell
  through an `env:` mapping and are consumed as quoted `"$VAR"`. An expression
  interpolated into a shell line is an injection site.
- **Job-level `permissions:` replaces the workflow-level block, not merges it.**
  So `nightly-deep`'s issue job and both deploy jobs restate `contents: read`
  alongside their escalation (`issues: write`, `id-token: write`).

### Deploy order is the contract

In `deploy-staging` the order is `deploy → smoke → promote`, and the promote
step carries no `if:` and no `continue-on-error:`, so a failed smoke gate writes
**no** promotion record. In both deploy templates the artifact digest is bound
once in `env:` — the digest that is gated is the digest that deploys — and the
`CONSUMER DEPLOY STEP` you replace must bind both `"$SURFACE"` and
`"$ARTIFACT_DIGEST"`.

`deploy-prod` adds a trust chain on top: the run must sit on the default branch
(refused explicitly before any grant is checked), the `{{PROD_ENV}}` GitHub
Environment's required reviewers approve the run and thereby bind
`github.sha`, and the job checks out that approved sha before
`check-grant --require-environments` runs from its bytes. Reviewer protection
on that environment is an **external prerequisite** — the template never
creates it.

Rollback is emitted, never executed: on failure the step writes the surface and
previous digest to `GITHUB_OUTPUT` and a note to the step summary. Executing it
goes back through the granted deploy flow.

## Test tiers

`scripts/gsd/test-tier.sh` is the single source of CI test commands. Workflows
never hardcode one.

```bash
scripts/gsd/test-tier.sh <fast|full|nightly|live> [registry]
```

- **stdout** — only the registered command lines, in declaration order (a tier
  with duplicate rows prints all of them, in order).
- **stderr** — usage and every diagnostic.
- **exit** — `0` lookup ok (a known tier with zero rows prints nothing and still
  exits 0); `2` usage error or unknown tier token; `3` registry missing or
  unreadable, with a one-line reason.

The two-step form in the templates exists for one reason:

```yaml
run: bash scripts/gsd/test-tier.sh fast > "$RUNNER_TEMP/tier-cmds" && bash -e "$RUNNER_TEMP/tier-cmds"
```

A tier lookup failure (rc 2/3) fails the job **distinctly** from a test failure.
Unlike `review-tier.sh`, `test-tier.sh` deliberately does not fail safe to exit
0 — a missing registry must redden the job, never silently run nothing.

Register tiers in `config/environments.yaml` under `test_tiers`; see
[Environment registry](environment-registry.md).

## Review tiers (a different thing, same idiom)

`scripts/gsd/review-tier.sh` sizes the **review** pipeline to a diff's risk so a
two-file docs change does not pay for a forty-file auth review. It classifies
only; it decides nothing about the honest-verifier pass.

```bash
scripts/gsd/review-tier.sh [--staged | --all | --file <path>]
```

`--staged` is the default (falling back to `git diff HEAD` when the index is
empty); `--all` diffs `<base>...HEAD` with `REVIEW_TIER_BASE` (default `main`);
`REVIEW_TIER` overrides detection outright. stdout is exactly one line,
`<tier> <reason>`.

| Tier | What `/review-gate` runs |
|---|---|
| `light` | Pass 1 only |
| `standard` | Passes 1–3 |
| `full` | Passes 1–3, a mandatory refute-or-promote round on every HIGH/CRITICAL, plus one extra cross-model adversary |

Failure is **fail-safe to `standard`, never `light`** — under-review beats
over-trust. A hostile `REVIEW_TIER_BASE` is rejected by
`git rev-parse --verify --end-of-options` before it can reach `git diff` as an
option.

## Promote emission

```bash
scripts/gsd/promote-emit.sh <run_id>
```

Prints the staging→prod promote command for each `deploy:staging-*` grant on a
run — one fully-formed line per grant, in `lib/gates.py`'s exact flag shape. It
**emits and never executes**, and has no store-write path at all.

Candidates come only from the evidence store's `_promotions[run_id]` records,
the sole run+surface binding the schema has. There is deliberately no fallback
over unclaimed top-level evidence: an unbound passing artifact from another run
could otherwise be emitted for this one. Latest by `recorded_at` wins; every
superseded candidate is named on stderr. A run with a staging grant but no
recorded promotion gets a value-free `PROMOTE-EMIT-ADVISORY` and exit 0.

Any store value that fails its shape gate produces a typed
`PROMOTE-EMIT-REFUSED` naming the field and the expected shape — and nothing is
emitted.

## Related

- [Environment registry](environment-registry.md)
- [Promotion protocol](promotion-protocol.md)
- [Commands](commands.md)
