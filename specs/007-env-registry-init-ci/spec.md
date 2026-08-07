# Spec 007 — Environment registry + /ffs-init + CI scaffolding + test tiers

Status: draft → decomposed | Branch: `007-env-registry-init-ci` | Run: `spec-007`
Prior art: `.planning/prior-art/spec-007-env-registry-init-ci.md` (opus design
agent, 2026-08-07 — REQ tables fed verbatim) + `specs/007-env-registry-init-ci/prior-art.md`.

## Context

FFS's promotion machinery (`gates.py check-grant` prod path) accepts a
`--manifest` no caller passes — the staging-counterpart check is vacuous in
every real run. Consumer repos have no declared environment model at all, so
autonomous runs discover missing env/secrets/deploy targets at 3am instead of
init time. Design verified against `lib/gates.py`:
`_parse_parity_manifest_yaml` (gates.py:1408-1446) already parses a combined
file — ONE committed `config/environments.yaml` needs no new parser, no
generator, no drift.

Core value: an autonomous run knows every environment, secret NAME, deploy
target, and test tier from a single committed registry — proven at init while
the operator is present, enforced at the prod boundary, never blocking repos
that haven't migrated.

## Non-negotiables (operator/design-locked — do not re-litigate)

- ONE committed file `config/environments.yaml` (parser already permits) — no
  derived manifest, no generator.
- /ffs-init is a SKILL, not a setup.sh flag; installer gains one stdout hint
  line, zero interactivity.
- One explicit batched init session — NOT a marker chain.
- NO timed migration window: un-migrated repos warn forever (one stderr line,
  prod path only); hard mode is opt-in (`--require-environments` /
  `FFS_ENV_REGISTRY_REQUIRED=1`).
- NEVER edit/merge/delete existing consumer workflows — generate only
  `ffs-*.yml`; collision → `ffs-<name>.ffs-proposed.yml` + printed diff.
- Secrets by NAME only, everywhere. Detection never reads `.env` file contents
  (key names via `grep -oE '^[A-Z_]+='` only).
- `PROD_ACTION_PREFIXES` untouched (security-model change ≠ config spec).
- Out of scope: infra provisioning, secret values, non-GitHub CI, general
  YAML parser, live drift detection (separate spec; registry is its input).

## Requirements

### Phase 1 — Registry tracer (gates.py becomes non-vacuous)

- REQ-101: committed `config/environments.yaml` (schema id `ffs.environments/v1`,
  header comment `SECRETS BY NAME ONLY`) with three blocks: `environments:`
  (agent-read; NO general YAML parse — the mechanically consumed fields
  (secret_names, base_url, verified, test_tier) are FLAT SCALARS extracted by
  the same hardened line-discipline as `test_tiers`, every other field is
  prose for agents — per-env name, kind(local|dev|staging|prod), base_url,
  secret_store{provider: none|doppler|gh-environment|sops|aws-sm, scope NAME
  only}, secret_names[] names-only, verified date (null = declared-unproven;
  >90d = advisory never a gate), deploy_command, test_tier,
  github_environment), `test_tiers:` (flat `- tier:` / `command:` scalar rows
  plus optional `covers:` glob list naming the suites the tier runs; repeated
  tiers run in order), and
  indent-0 `surfaces:` (SECURITY INPUT parsed verbatim by gates.py —
  `- surface:` / `staging_instance:`; `none` = explicit no-staging,
  fail-closed; repo with no prod surfaces omits the block). FFS's OWN registry
  ships in this phase as the tracer.
- REQ-102: gates.py default resolution — `--manifest` absent resolves
  `$FFS_ENV_REGISTRY` → `config/environments.yaml` →
  `config/parity-manifest.yaml`; all absent → today's behavior plus exactly
  ONE stderr line `ENV-REGISTRY-ABSENT: ... (run /ffs-init)`. Fail-closed
  rules (gauntlet CRITICAL/HIGH adoptions): (a) a RESOLVED registry that
  fails to parse is CHECK-GRANT-REJECTED for prod actions regardless of
  require-mode — only file-not-found is "absent"; (b) `$FFS_ENV_REGISTRY`
  set but unresolvable (nonexistent/unreadable) is REJECTED on prod actions,
  never silently absent; (c) `--manifest ""` (present-but-empty) is REJECTED,
  distinguished from flag-absent; (d) resolution anchors to the MAIN
  checkout (same `git rev-parse --git-common-dir` pin as the evidence store —
  a worktree-local uncommitted registry must not govern its own prod gate)
  and the resolved registry file must be git-TRACKED (`git ls-files
  --error-unmatch`); untracked → treated as parse-failure (REJECTED on prod).
  Hard mode (`--require-environments` / `FFS_ENV_REGISTRY_REQUIRED=1`,
  prod-prefix actions only): requires a committed `ffs.environments/v1`
  registry — legacy `parity-manifest.yaml` satisfies soft mode only; absent
  or non-v1 → CHECK-GRANT-REJECTED with `/ffs-init` remedy; a prod surface
  with no exact `surfaces:` row → `UNKNOWN-PROD-SURFACE` REJECTED (unknown ≠
  safe). Soft mode keeps unknown-surface behavior unchanged (advisory only).
  Surface matching casefolds both sides (`deploy:prod-Web` cannot bypass the
  `web` row); the `none` sentinel matches casefolded+stripped. Scope stated
  plainly: require-mode covers `PROD_ACTION_PREFIXES` actions only —
  `hotfix:prod-*` routes before manifest logic today and stays unchanged
  (posture-knob domain, spec 006/008). Explicit valid `--manifest` always
  wins; JSON manifests still load.
- REQ-103: parser hardening (existing parser hardened — no NEW parser) —
  `surfaces:` trigger requires indent==0 (a nested `surfaces:` key must not
  flip parsing); flush-and-clear replaces break AND sets the parse
  single-entry (after the surfaces block closes, `in_surfaces` stays False
  forever — later indented `- surface:` lines, e.g. inside the
  `environments:` prose block, are never parsed; fixture pins `environments:`
  placed AFTER `surfaces:`); within the surfaces block, duplicate surface
  keys, duplicate blocks, tab indentation, and unknown row fields are
  REJECTED (fail-closed — never "deterministic either-way"); stale docstring
  at `_surface_has_staging` (gates.py:975-979, the "wiring lands Phase 4"
  text) corrected — NOT :890-894 as the design doc said.
- REQ-104: refusal reasons surface — `check_grant_prod` returns/records a
  typed reason and the CLI prints it (`NO-STAGING-COUNTERPART: <surface> —
  remedy: add staging_instance for surface <surface> in
  config/environments.yaml`, `UNKNOWN-PROD-SURFACE: ...`,
  `ENV-REGISTRY-INVALID: ...`); today the reason lives only in the pending
  record while the CLI prints generic text (gates.py:1932) — a fourth
  surgical edit surfaces it. Every refusal line names its remedy.

### Phase 2 — /ffs-init

- REQ-201: `skills/ffs-init/SKILL.md` — one batched interactive session with
  three question blocks mirroring the registry's own structure (block 1 =
  `environments` rows, block 2 = `test_tiers` classification confirm/edit,
  block 3 = `surfaces` — isolated as its own gate because gates.py parses it
  verbatim) PLUS first-class non-interactive modes: `--detect-only` (writes
  NOTHING; proposal YAML with per-row `confidence:` + `evidence:`),
  `--answers <file.yaml>` (fully non-interactive; missing required key →
  nonzero naming key + expected shape, nothing written), `--yes` (accept
  detected defaults, `verified: null` everywhere), `--update` (diff against
  an existing registry: adds/flags NEW rows only, preserves operator-set
  `verified` dates and manual edits — re-running `--yes` over an existing
  registry must not silently regenerate it), `--check` (read-only validation
  — schema, secret-value leak scan per REQ-202a, referential integrity
  (unique environment names; every `staging_instance` value references a
  declared `kind: staging` environment), stale-verified advisory, surfaces
  block parses under gates.py, tier coverage per REQ-303; nonzero on schema
  violation, leak, or referential break; CI-runnable; when `gh` is
  authenticated, an ADVISORY probe checks the `{{PROD_ENV}}` GitHub
  environment has reviewer protection — external prerequisite, never a
  gate). Every failure path names its remedy (expected-vs-got, corrective
  action — not just location).
- REQ-202a: leak scan contract — detector families: hex runs ≥32 chars,
  base64 runs ≥40 chars, `password=`/`token=`/`secret=` assignments,
  credential-bearing URLs (`scheme://user:pass@`), provider token prefixes
  (`AKIA`, `ghp_`, `gho_`, `github_pat_`, `sk-`, `xox[bp]-`), JWT shape
  (`eyJ` + two dots), PEM blocks (`-----BEGIN`). Context whitelist: `uses:
  <owner>/<repo>@<40-hex>` action pins and `sha256:<64-hex>` artifact digests
  are NOT findings. Output contract: `line N, key <name>, shape <class> —
  remedy: replace the literal with a NAME in secret_names` — matched bytes
  NEVER appear on stdout or stderr (test asserts the fixture's secret
  substring absent from both). Already-committed finding → remedy text
  states rotate-then-rewrite-history. Pre-commit hook: out of scope
  (accepted residual — CI `--check` is the enforcement point; repo has no
  pre-commit infra).
- REQ-202: detection heuristics are read-only and propose-never-decide
  (`verified: null` + evidence comment per row): vercel.json/.vercel →
  prod+preview; wrangler.toml `[env.X]` → env per section; fly.toml +
  fly.X.toml → env per file; docker-compose → local; k8s/kustomize overlays →
  env per dir; `.env.staging`/`.env.production` → env row + secret KEY NAMES
  only; workflow `environment:` keys; `gh api repos/{o}/{r}/environments`
  (skipped unauthenticated, fail-soft); doppler.yaml → provider + config
  names; nothing found → local-only registry, surfaces omitted. Tier
  classification proposal: `-e2e|-live|browser|canary` → live; self-skipping
  on missing vendor tree / GSD_* guard → nightly; pytest-unit-shaped → fast;
  else full.
- REQ-203: `scripts/gsd/env-registry.sh <detect|check|render|apply>` backs
  the skill — `apply` is the single atomic writer (consumes an answers file,
  writes registry + `.ffs-init.json` all-or-nothing; the SKILL only collects
  answers, never writes the registry itself). Operator declines persist in
  `.ffs-init.json` (schema `ffs.init/v1`, same convention as
  `.ffs-socratic.json`); a decline is keyed to (heuristic, concrete evidence
  value) — NEW evidence under a previously-declined heuristic re-proposes;
  `--reset-declines` clears them. Reset path documented (`--force`
  regenerates from scratch after explicit confirm).

### Phase 3 — CI templates + tiering

- REQ-301: five templates under `templates/ci/` with placeholders
  (`{{TIER_*}}`, `{{STAGING_ENV}}`, `{{PROD_ENV}}`, `{{LOCKFILE_HASH_PATH}}`)
  rendered by `env-registry.sh render`: `pr-fast.yml` (pull_request;
  concurrency + cancel-in-progress, workflow-level `permissions:
  contents: read`, fetch-depth 1, persist-credentials false, SHA-pinned
  actions + version comment, cache keyed on hashFiles(lockfile), timeout 10,
  fast tier only), `main-full.yml` (push main; + needs-chaining,
  fail-fast:false matrix, artifact retention 7, timeout 30),
  `nightly-deep.yml` (cron + dispatch; nightly tier, timeout 60, failure
  opens an issue instead of reddening main), `deploy-staging.yml`
  (workflow_call + dispatch; environment {{STAGING_ENV}}, job-level
  `id-token: write` OIDC, consumes an artifact DIGEST input and never
  rebuilds; ORDER IS THE CONTRACT: deploy → post-deploy smoke recorded as
  artifact-bound evidence (`gates.py run-gate ... --artifact <digest>`) →
  only then `gates.py promote --from staging --to prod ... --evidence <id>`;
  smoke failure writes NO promotion record — test asserts it),
  `deploy-prod.yml` (workflow_call; environment {{PROD_ENV}} — reviewer
  protection is an EXTERNAL prerequisite validated only by the `--check`
  advisory probe, the template cannot create it; `gates.py check-grant
  --action deploy:prod-<surface> --artifact <digest> --require-environments`
  BEFORE any mutation; post-deploy smoke; on smoke failure the job FAILS and
  EMITS the rollback command (previous digest) as output — it never executes
  rollback itself: automatic rollback is a prod mutation with no typed grant
  and `PROD_ACTION_PREFIXES` is frozen). Deploy templates declare validated
  `workflow_call` inputs tied to registry rows (surface, artifact digest,
  previous digest, smoke command).
- REQ-302: anti-clobber is structural — render writes only
  `.github/workflows/ffs-<name>.yml`; name collision → proposal written to
  `.github/ffs-proposals/<name>.yml` (OUTSIDE the workflows dir — any
  `.yml`/`.yaml` under `.github/workflows/` is an EXECUTABLE workflow, so a
  proposal there would run; gauntlet HIGH) + print `diff -u`; existing
  workflows are never edited, merged, or deleted (byte-identity of originals
  asserted in tests). Render additionally emits `.github/dependabot.yml` with a
  `github-actions` ecosystem entry when the file is absent (new file —
  anti-clobber-safe; pins rot without it), and when a dependabot.yml already
  exists WITHOUT that ecosystem, prints an advisory instead of editing it
  (prior-art judge adoption).
- REQ-303: `scripts/gsd/test-tier.sh <fast|full|nightly|live> [registry]`
  (mirrors review-tier.sh idiom) prints one command per line from
  `test_tiers:`; unknown tier → exit 2; missing registry → exit 3 + reason.
  Workflows call it; no workflow hardcodes a test command.
  `env-registry.sh check` asserts every discovered suite matches ≥1 tier's
  `covers:` globs (deterministic glob match over discovered test files/dirs —
  not inference from opaque command strings); a suite matching nothing fails
  the check naming the suite and the nearest tier.

### Phase 4 — Seam wiring + docs

- REQ-401: seams — feature-implement Step-2 advisory (hard stop ONLY under
  `--autonomous` + prod action in plan; prod actions carry `--artifact`);
  preflight SKILL seeds its manifest from registry `secret_names` + base_url
  probes (names-only, additive-never-authoritative); finish tail EMITS (never
  runs) the promote command after a staging-deploy grant is consumed;
  promotion-protocol rules 3/4/5 gain concrete enforcement columns
  (path + command); installer prints the one hint line when registry absent;
  repo CI lint block runs `env-registry.sh check`.
- REQ-402: an absent registry NEVER hard-breaks a non-prod run — advisory
  only, exit codes unchanged (pinned by test).
- REQ-403: every gate in this spec is fixture/stub-driven — zero cloud, zero
  live network, zero credentials in any test.

## BDD Scenarios

Feature: a repo declares its environments once and every FFS gate consumes the declaration

Scenario: prod gate becomes non-vacuous the moment the registry exists
  Given a repo with `config/environments.yaml` whose `web` surface declares `staging_instance: none`
  When check-grant evaluates `deploy:prod-web` without any `--manifest` flag
  Then the action is rejected with `NO-STAGING-COUNTERPART` naming the `web` surface

Scenario: un-migrated repo keeps working
  Given a repo with no `config/environments.yaml` and no `config/parity-manifest.yaml`
  When check-grant evaluates a prod action without `--require-environments`
  Then the exit code matches today's behavior exactly and stderr carries the single line `ENV-REGISTRY-ABSENT`

Scenario: hard mode refuses unmigrated prod deploys
  Given a repo with no registry and `FFS_ENV_REGISTRY_REQUIRED=1`
  When check-grant evaluates `deploy:prod-web`
  Then the result is CHECK-GRANT-REJECTED naming the missing registry and the `/ffs-init` remedy

Scenario: detection proposes without writing
  Given a repo containing `vercel.json` and `.env.production`
  When the operator runs `/ffs-init --detect-only`
  Then a proposal with per-row confidence and evidence is printed, `git status --porcelain` is empty, and secret rows carry key NAMES only

Scenario: leak scan blocks a poisoned registry
  Given a registry draft containing a 40-char hex string in a `secret_names` row
  When the operator runs `/ffs-init --check`
  Then the check exits nonzero naming the offending row shape without printing the value itself

Scenario: scaffold never touches existing workflows
  Given a repo whose `.github/workflows/` already contains `ffs-pr-fast.yml` and 33 unrelated workflows
  When `env-registry.sh render` scaffolds CI
  Then the proposal lands as `ffs-pr-fast.ffs-proposed.yml` with a printed diff, and every pre-existing workflow is byte-identical

Scenario: tier lookup drives CI
  Given a registry with two `fast` tier rows in order
  When a workflow calls `test-tier.sh fast`
  Then both commands print in declaration order and an unknown tier exits 2

## Acceptance Criteria

- AC-001 (REQ-101/102/104): new pytest cases green — default resolution
  found/absent, `$FFS_ENV_REGISTRY` precedence + set-but-broken → REJECTED,
  explicit `--manifest` overrides + empty-value → REJECTED,
  `staging_instance: none` (incl. case/whitespace variants) →
  NO-STAGING-COUNTERPART asserted via BOTH the pending record's typed reason
  AND the CLI-printed reason line, casefolded surface matching, unparseable
  resolved registry → REJECTED both modes, untracked/worktree-only registry
  → REJECTED, omitted surfaces + `--require-environments` →
  UNKNOWN-PROD-SURFACE REJECTED, hard mode not satisfied by legacy
  parity-manifest, absent registry → byte-identical exit + one stderr
  advisory.
- AC-002 (REQ-103): adversarial parser fixtures green — nested `surfaces:`
  does not flip parsing, surfaces-not-last parses, dup keys, tabs, CRLF.
- AC-003 (REQ-101): live `check-grant` against FFS's OWN committed registry —
  invoked with a valid `--artifact` (the manifest check sits after the
  artifact guard) and an ISOLATED evidence store (every rejection writes a
  pending record; the real store must not be mutated by tests) — exits 1
  with the `NO-STAGING-COUNTERPART` reason line for a `none`-surface prod
  action; `test_promotion_protocol_doc` green; pytest baseline (recorded
  pre-phase-1) unchanged.
- AC-004 (REQ-201/202): bats fixture trees (vercel, wrangler, fly, compose,
  k8s, bare) each emit the documented rows + evidence; `.env` fixtures yield
  keys, never values; `--detect-only` leaves `git status --porcelain` empty;
  `--answers`/`--yes` complete with zero prompts.
- AC-005 (REQ-201/203): `--check` exits 0 on FFS's registry, nonzero on a
  secret-shaped fixture; `shellcheck -S warning scripts/gsd/env-registry.sh`
  clean; `verify-skill-blocks.py` + `lint_host_dispatch.py` green.
- AC-006 (REQ-301): `test_ci_templates.py` green — workflow-level
  permissions present, timeout on every job, every `uses:` pinned to 40-hex
  SHA + version comment, concurrency on pr/push templates, fetch-depth 1,
  deploy-prod carries `environment:` + `check-grant --require-environments`
  before any mutation step.
- AC-007 (REQ-302): ci-scaffold.bats — collision → `.ffs-proposed` + original
  `cmp`-identical; empty repo gets exactly 5 `ffs-*.yml`.
- AC-008 (REQ-303): test-tier.bats — declaration order, unknown tier → 2,
  missing registry → 3 + reason; unclassified-suite check fails.
- AC-009 (REQ-401): integration cases — finish-tail promote-command EMIT (not
  run), review-gate inherits with NO change to review-gate-command.sh,
  preflight seeding additive-never-authoritative, installer hint test.
- AC-010 (REQ-402): absent-registry non-prod run exit codes pinned unchanged.
- AC-011 (REQ-403): grep-level gate — no test touches network (fixtures/stubs
  only); no credential-shaped string in any fixture EXCEPT under
  `tests/fixtures/leak-scan/` (path-exempted — those fixtures exist to prove
  the scan fires; values in them are synthetic and labeled).

## E2E Test Paths

- PATH-001: bare repo → `/ffs-init --detect-only` → proposal printed, nothing
  written → `--yes` → registry committed → `--check` exits 0.
- PATH-002: registry present → `check-grant deploy:prod-web` (no flags) →
  NO-STAGING-COUNTERPART → operator adds staging surface → grant proceeds to
  the artifact/promote checks.
- PATH-003: `env-registry.sh render` on a repo with existing workflows →
  5 proposals/files, originals byte-identical → CI calls `test-tier.sh fast`
  → declared commands run.
- PATH-004: un-migrated consumer repo → full feature-implement run → single
  stderr advisory, zero behavior change → `FFS_ENV_REGISTRY_REQUIRED=1` →
  prod action refused with remedy.
- PATH-005: registry with stale `verified:` (>90d) → `--check` prints
  advisory, exits 0 (never a gate).

## Edge Cases

- EDGE-001: nested `surfaces:` key inside an `environments:` entry → parser
  ignores it (indent-0 trigger); adversarial fixture pins it.
- EDGE-002: `surfaces:` block NOT last in the file → parses (flush-not-break).
- EDGE-003: `.env.production` present but unreadable → detection row emitted
  with `evidence: unreadable`, no crash, no content read.
- EDGE-004: `gh` unauthenticated → environments probe skipped silently,
  other heuristics still run (fail-soft).
- EDGE-005: registry file present but schema-invalid → `--check` nonzero with
  row-level expected-vs-got reason; gates.py treats a RESOLVED-but-unparseable
  registry as CHECK-GRANT-REJECTED for prod actions in BOTH modes (fail
  closed — a hostile YAML-breaking edit must not delete the gate; gauntlet
  CRITICAL). Non-prod actions unaffected.
- EDGE-006: two `- tier: fast` rows → both run, declaration order (pinned).
- EDGE-007: collision where existing `ffs-pr-fast.yml` is byte-identical to
  the render → no `.ffs-proposed`, print `up-to-date`, exit 0.
- EDGE-008: `--answers` file missing a required key → exit nonzero naming the
  key; nothing written (all-or-nothing).
- EDGE-009: monorepo with vercel.json + wrangler.toml + fly.toml → all rows
  proposed with per-row evidence; `--detect-only` output is reviewable, none
  auto-accepted.
- EDGE-010: `FFS_ENV_REGISTRY` set but pointing at a nonexistent/unreadable
  path → prod actions REJECTED naming the broken var + remedy ("unset it or
  point at a valid tracked file"); never silently falls through (an env var
  is an unaudited control channel — one-word gate-disable forbidden).
- EDGE-011: `--manifest ""` (empty value) → REJECTED, distinguished from
  flag-absent (falsy-string fall-through forbidden).
- EDGE-012: registry exists only in the agent worktree (uncommitted/untracked)
  → prod actions REJECTED (main-checkout + tracked-file anchoring).
- EDGE-013: `environments:` block placed AFTER `surfaces:` containing
  indented `- surface:`-shaped prose lines → never parsed (single-entry
  parse); fixture pins it.
- EDGE-014: `staging_instance: NONE` / `'none '` / `None` → all match the
  sentinel (casefold+strip); `deploy:prod-Web` matches the `web` row.

## Test Contract Summary

| Layer             | Count | Status  |
|-------------------|-------|---------|
| BDD Scenarios     | 7     | draft   |
| Unit test cases   | in plan.md Unit Test List | listed |
| Unit test files   | 3 (test_gates env-registry cases, test_ci_templates.py, existing suites) | mapped |
| Integration tests | 4 (INT rows in plan.md) | defined |
| E2E paths         | 5     | defined (CLI journeys — no browser surface, Playwright N/A) |
