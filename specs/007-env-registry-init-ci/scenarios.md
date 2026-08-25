# Scenarios — spec 007 (stable IDs for phase QA gates)

CLI-surface spec: scenarios are shell-runthrough-able, no browser.

## US1 — a repo declares environments once, gates consume the declaration

- US1-S1 (happy): Given a repo whose registry declares surface `web` with
  `staging_instance: staging-web`, When check-grant evaluates
  `deploy:prod-web` with artifact + grant + promote record present, Then the
  staging-counterpart check passes and the verdict proceeds to the remaining
  prod checks.
- US1-S2 (refusal): Given `staging_instance: none` for surface `web`, When
  check-grant evaluates `deploy:prod-web` with no `--manifest` flag, Then the
  verdict is NO-STAGING-COUNTERPART naming `web`.
- US1-S3 (unmigrated): Given no registry files exist, When check-grant
  evaluates a prod action without require-mode, Then behavior is
  byte-identical to pre-007 plus exactly one `ENV-REGISTRY-ABSENT` stderr
  line.
- US1-S4 (hard mode): Given no registry and `FFS_ENV_REGISTRY_REQUIRED=1`,
  When check-grant evaluates `deploy:prod-web`, Then CHECK-GRANT-REJECTED
  with the `/ffs-init` remedy.

## US2 — an operator initializes the registry with evidence, never leaks

- US2-S1 (detect): Given a repo containing vercel.json and .env.production,
  When the operator runs `/ffs-init --detect-only`, Then a proposal with
  per-row confidence + evidence prints, the worktree stays clean, and secret
  rows carry key NAMES only.
- US2-S2 (accept): Given a reviewed proposal, When the operator runs
  `/ffs-init --yes`, Then `config/environments.yaml` is written with
  `verified: null` rows and `--check` exits 0 on it.
- US2-S3 (leak refusal): Given a registry draft containing a 40-hex value in
  a name field, When `--check` runs, Then it exits nonzero naming the row
  SHAPE without echoing the value.
- US2-S4 (declines persist): Given the operator declined an environment row,
  When `/ffs-init` re-runs, Then the declined row is not re-asked
  (`.ffs-init.json`).

## US3 — CI scaffolds without touching what exists

- US3-S1 (fresh scaffold): Given a repo with no ffs workflows, When
  `env-registry.sh render` runs, Then exactly five `ffs-*.yml` land plus a
  `dependabot.yml` github-actions entry when absent.
- US3-S2 (collision): Given `ffs-pr-fast.yml` already exists with different
  content, When render runs, Then the new version lands as
  `.github/ffs-proposals/ffs-pr-fast.yml` (outside the executable workflows
  dir) with a printed diff and every pre-existing workflow is byte-identical.
- US3-S3 (tier lookup): Given a registry with two `fast` rows, When a
  workflow calls `test-tier.sh fast`, Then both commands print in declaration
  order; unknown tier exits 2.
- US3-S4 (unclassified suite): Given a discovered test suite in no tier,
  When `env-registry.sh check` runs, Then it exits nonzero naming the suite.
