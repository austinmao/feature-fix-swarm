# Plan 007 — Environment registry + /ffs-init + CI scaffolding + test tiers

Source spec: `specs/007-env-registry-init-ci/spec.md`. Design source:
`.planning/prior-art/spec-007-env-registry-init-ci.md` (verified against
lib/gates.py; line anchors re-verified 2026-08-07 on main@a8866f7 — parser
moved since the design doc: `_parse_parity_manifest_yaml` now at
gates.py:1517, `surfaces:` trigger at :1532-1534, `--manifest` flag read at
:1920, `check_grant_prod` at :1069, usage banner :42; phase research re-pins
exact edit sites before any change).

## Prior-art decision

Per `specs/007-env-registry-init-ci/prior-art.md`: **build-fresh**. Zero
registry-shaped OSS prior art above threshold; the one partial candidate
(step-security/secure-repo, 329★) rewrites existing workflows — structurally
forbidden by REQ-302 — and its SHA-pinning value is captured by authoring
templates with baked-in pinned SHAs instead of runtime pinning logic;
reference-only. Real adoption is LOCAL: `_parse_parity_manifest_yaml` (no NEW
parser — the existing parser is hardened per REQ-103 and the registry is
designed to parse under it), `review-tier.sh` argv/env idiom for
test-tier.sh, `.ffs-socratic.json` convention for `.ffs-init.json`,
`templates/` conventions.

## Architecture (from the locked design)

- ONE committed `config/environments.yaml` (`ffs.environments/v1`):
  `environments:` prose block (unparsed by machines — YAGNI on a parser until
  a mechanical consumer exists), `test_tiers:` flat scalar rows (awk-consumed
  by test-tier.sh), indent-0 `surfaces:` (the ONLY security-input block,
  parsed verbatim by the existing gates.py parser).
- gates.py: three surgical edits — (1) default manifest resolution
  (`$FFS_ENV_REGISTRY` → `config/environments.yaml` →
  `config/parity-manifest.yaml`; absent → unchanged behavior + one
  `ENV-REGISTRY-ABSENT` stderr line), (2) `--require-environments` /
  `FFS_ENV_REGISTRY_REQUIRED=1` hard mode (prod-prefix only), (3) parser
  hardening (indent-0 `surfaces:` trigger, flush-and-clear not break,
  docstring fix).
- `/ffs-init` SKILL + `scripts/gsd/env-registry.sh <detect|check|render>`;
  modes `--detect-only` / `--answers` / `--yes` / `--check`; declines in
  `.ffs-init.json`.
- `templates/ci/` ×5 with `{{PLACEHOLDER}}`s; render = anti-clobber
  (`ffs-*.yml` only; collision → `.ffs-proposed.yml` + diff).
- `scripts/gsd/test-tier.sh <tier> [registry]` — the single source of test
  commands for workflows.
- Seams: feature-implement Step-2 advisory, preflight seeding
  (additive-never-authoritative), finish-tail promote EMIT, promotion-protocol
  rules 3/4/5 enforcement columns, installer hint, CI lint `env-registry.sh
  check`.

## Unit Test List

Sequenced design-critical first (each RED before implementation):

- [ ] check_grant_prod: no --manifest + registry present → registry resolved, `staging_instance: none` surface → NO-STAGING-COUNTERPART
- [ ] check_grant_prod: no --manifest + no registry files → exit/verdict byte-identical to today + exactly one ENV-REGISTRY-ABSENT stderr line
- [ ] resolution precedence: $FFS_ENV_REGISTRY beats config/environments.yaml beats config/parity-manifest.yaml; explicit --manifest beats all
- [ ] $FFS_ENV_REGISTRY → nonexistent path: treated absent, advisory names the env var
- [ ] --require-environments + absent registry + prod-prefix action → CHECK-GRANT-REJECTED with /ffs-init remedy
- [ ] --require-environments + absent registry + NON-prod action → unchanged (prod-prefix only)
- [ ] FFS_ENV_REGISTRY_REQUIRED=1 equivalent to the flag
- [ ] parser: nested `surfaces:` (indent>0) does NOT enter surfaces mode
- [ ] parser: surfaces block not last in file → parses (flush-and-clear)
- [ ] parser: dup surface keys, tab-indented rows, CRLF line endings → deterministic reject-or-parse per fixture, never a crash
- [ ] parser: unparseable surfaces + require-mode → REJECTED; without require-mode → ABSENT + advisory
- [ ] test-tier.sh: two fast rows → both printed, declaration order
- [ ] test-tier.sh: unknown tier → exit 2; missing registry → exit 3 + reason
- [ ] env-registry.sh check: FFS's own registry → 0; secret-shaped fixture (40-hex, base64 run, password=, credential URL) → nonzero, value NOT echoed
- [ ] env-registry.sh check: stale verified (>90d) → advisory line, exit 0
- [ ] env-registry.sh check: unclassified discovered suite → nonzero naming the suite
- [ ] detect: each fixture tree (vercel/wrangler/fly/compose/k8s/bare) → documented rows + evidence
- [ ] detect: .env.production fixture → key NAMES only, values never in output
- [ ] detect: --detect-only → git status --porcelain empty
- [ ] detect: --answers missing required key → nonzero naming key, nothing written
- [ ] render: empty repo → exactly 5 ffs-*.yml
- [ ] render: collision → .ffs-proposed.yml + original cmp-identical; byte-identical existing → up-to-date, no proposal
- [ ] render: absent dependabot.yml → emitted with github-actions ecosystem; existing dependabot.yml without that ecosystem → advisory printed, file untouched
- [ ] test_ci_templates: every job timeout, workflow-level permissions, 40-hex pinned uses + comment, concurrency (pr/push), fetch-depth 1, deploy-prod environment + pre-mutation check-grant --require-environments
- [ ] check_grant_prod: resolved-but-unparseable registry → REJECTED both modes; untracked/worktree-only registry → REJECTED; casefolded surface + sentinel matching; hard mode unsatisfied by legacy parity-manifest; UNKNOWN-PROD-SURFACE under require-mode
- [ ] check_grant_prod CLI: typed reason line printed (NO-STAGING-COUNTERPART/UNKNOWN-PROD-SURFACE/ENV-REGISTRY-INVALID) with remedy — asserted against stdout AND the pending record
- [ ] parser: environments-block-after-surfaces fixture → its `- surface:`-shaped lines never parsed (single-entry); dup keys/blocks/tabs/unknown fields → REJECT
- [ ] leak scan: each detector family fires on its fixture; uses:@40hex + sha256:64hex whitelisted; secret substring absent from stdout+stderr
- [ ] apply: atomic all-or-nothing write; declines keyed (heuristic, evidence); new evidence re-proposes; --reset-declines
- [ ] render: proposals land in .github/ffs-proposals/, never .github/workflows/*.ffs-proposed*
- [ ] deploy-staging template: smoke-fail → no promotion record (stub gates.py records calls)

## TDD Unit Test Map

| Source file | Test file | Functions/behaviors |
|-------------|-----------|---------------------|
| lib/gates.py (resolution + require-mode + parser) | lib/tests/test_gates.py (new `TestEnvRegistry*` cases) | default resolution chain, advisory single-line, require-mode prod-only, parser hardening fixtures |
| scripts/gsd/test-tier.sh | tests/bats/test-tier.bats | order, unknown→2, missing→3 |
| scripts/gsd/env-registry.sh | tests/bats/env-registry.bats | detect fixtures, check leak-scan, check tiers-coverage, render anti-clobber |
| templates/ci/*.yml | tests/test_ci_templates.py | structural assertions over rendered output |
| skills/ffs-init/SKILL.md | scripts/verify-skill-blocks.py + lint_host_dispatch.py (existing) | verify-block + dispatch lint |
| config/environments.yaml (FFS's own) | live check-grant case in test_gates.py | NO-STAGING-COUNTERPART on none-surface |

## Integration Tests

- INT-001: live `check-grant deploy:prod-<none-surface>` against FFS's own
  committed registry (no flags) → exit 1 NO-STAGING-COUNTERPART.
- INT-002: finish-tail after a consumed staging-deploy grant EMITS (never
  runs) the promote command — stub run asserts the command string appears and
  no promote record is written.
- INT-003: preflight manifest seeded from registry secret_names is
  additive-never-authoritative — a manifest entry the registry lacks
  survives; registry rows append; names only.
- INT-004: review-gate-command.sh byte-unchanged; inherits resolution through
  gates.py (case pins no call-site edit happened).

## Phase Test Gates

| Phase | Gate condition | Command |
|-------|----------------|---------|
| P1 registry tracer | pytest incl. new env-registry cases; baseline unchanged | `python3 -m pytest lib/tests/test_gates.py tests/ -q` |
| P2 /ffs-init | + env-registry.bats; shellcheck; skill lints | `bats tests/bats/env-registry.bats && shellcheck -S warning scripts/gsd/env-registry.sh && python3 scripts/verify-skill-blocks.py && python3 scripts/lint_host_dispatch.py skills/*/SKILL.md` |
| P3 CI + tiers | + test-tier.bats, ci-scaffold assertions, test_ci_templates.py | `bats tests/bats/test-tier.bats && python3 -m pytest tests/test_ci_templates.py -q` |
| P4 seams | full cumulative suites + doc tests | `python3 -m pytest tests/ lib/ -q && bash scripts/gsd/gates-test-command.sh` |

## Phases (tracer-first)

- Phase 1 — Registry tracer: FFS's own `config/environments.yaml` + the three
  gates.py edits + doc columns. REQ-101..103. The tracer slice is
  end-to-end: committed registry → default resolution → live
  NO-STAGING-COUNTERPART on FFS itself.
- Phase 2 — /ffs-init: SKILL + env-registry.sh detect/check + `.ffs-init.json`.
  REQ-201..203 (render arrives P3 with the templates it renders).
- Phase 3 — CI templates + tiering: 5 templates + test-tier.sh +
  env-registry.sh render + classification. REQ-301..303.
- Phase 4 — Seam wiring + docs: feature-implement/preflight/finish-tail/
  promotion-protocol/installer/CI-lint seams. REQ-401..403.

## Decision Audit Trail (review gauntlet 2026-08-07 — codex CEO+Eng, opus adversarial eng, sonnet DX, opus prior-art judge)

| # | Source | Finding | Disposition |
|---|--------|---------|-------------|
| 1 | opus C1 | resolved-but-unparseable registry = absent+advisory fails OPEN | ADOPTED — REJECTED for prod in both modes; only file-not-found is absent (REQ-102a, EDGE-005) |
| 2 | opus C2 | flush-and-clear without single-entry re-opens parse to later blocks | ADOPTED — single-entry parse, environments-after-surfaces fixture (REQ-103, EDGE-013) |
| 3 | opus H3 + codex 3 | `none` sentinel exact-match; unknown surface = has-staging | ADOPTED — casefold+strip sentinel; require-mode UNKNOWN-PROD-SURFACE REJECTED (REQ-102, EDGE-014) |
| 4 | opus H4 | case-varied action surface bypasses row lookup | ADOPTED — casefold both sides (REQ-102) |
| 5 | opus H5 | $FFS_ENV_REGISTRY = unaudited one-word gate-disable | ADOPTED — set-but-unresolvable REJECTED on prod (REQ-102b, EDGE-010) |
| 6 | opus H6 | worktree-local/untracked registry governs its own prod gate | ADOPTED — main-checkout anchor (store's git-common-dir pin) + tracked-file requirement (REQ-102d, EDGE-012) |
| 7 | opus H7 | NO-STAGING-COUNTERPART reason never reaches CLI; AC unimplementable | ADOPTED — new REQ-104 fourth surgical edit; AC-001/003 restated |
| 8 | opus M8 | live AC needs --artifact + isolated store | ADOPTED (AC-003) |
| 9 | opus M9 | AC-011 grep gate blocks the leak fixtures it needs | ADOPTED — path exemption tests/fixtures/leak-scan/ (AC-011) |
| 10 | opus M10 + codex 13 | leak shapes too narrow; thresholds unnamed; post-hoc only | ADOPTED — REQ-202a detector families + thresholds + rotate-and-rewrite remedy; pre-commit hook = accepted residual (no pre-commit infra; CI enforces) |
| 11 | opus M11 | 40-hex heuristic flags the SHA pins the spec mandates | ADOPTED — context whitelist uses:@/sha256: (REQ-202a) |
| 12 | opus M12 | row-level reason one step from echoing the secret | ADOPTED — fixed output contract + substring-absence test (REQ-202a) |
| 13 | opus L13 | docstring anchor :890-894 wrong | ADOPTED — anchor corrected to _surface_has_staging :975-979 (REQ-103) |
| 14 | opus L14 | hotfix:prod-* bypasses require-mode silently | ADOPTED — scope stated in REQ-102; behavior unchanged (posture domain) |
| 15 | opus L15 | --manifest "" falsy fall-through | ADOPTED — empty = REJECTED (EDGE-011) |
| 16 | codex 1 | "unparsed" environments block vs mechanical consumers | ADOPTED — flat-scalar extraction contract for the 4 consumed fields (REQ-101) |
| 17 | codex 2 | hard mode satisfiable by legacy manifest/env-var | ADOPTED — hard mode requires committed v1 registry (REQ-102) |
| 18 | codex 4 | staging_instance = arbitrary string satisfies gate | ADOPTED — referential integrity in --check (unique env names; staging_instance references declared kind:staging env) (REQ-201); gates.py parser stays minimal by design |
| 19 | codex 5 | reject-or-parse ambiguity on security input | ADOPTED — dup keys/blocks/tabs/unknown fields REJECTED (REQ-103) |
| 20 | codex 6 | .ffs-proposed.yml inside workflows dir is EXECUTABLE | ADOPTED — proposals to .github/ffs-proposals/ (REQ-302) |
| 21 | codex 7 | promote before smoke; evidence not artifact-bound | ADOPTED — deploy→smoke(run-gate --artifact)→promote order; smoke-fail writes no promotion (REQ-301) |
| 22 | codex 8 | reviewer protection unverifiable from template | ADOPTED — external prerequisite + --check advisory probe when gh authed (REQ-201/301) |
| 23 | codex 9 | automatic rollback = ungranted prod mutation | ADOPTED — rollback EMITTED never executed; job fails (REQ-301) |
| 24 | codex 10 | deploy templates lack inputs for multi-surface/rollback | ADOPTED — validated workflow_call inputs tied to registry rows (REQ-301) |
| 25 | codex 11 | no deterministic owner for writes | ADOPTED — env-registry.sh apply, atomic, sole writer (REQ-203) |
| 26 | codex 12 | tier coverage unprovable from opaque commands | ADOPTED — optional covers: globs; deterministic glob match (REQ-101/303) |
| 27 | codex 14 | dependabot.yml = scope creep | REJECTED — prior-art judge adopted it for pin-rot; REQ-302-safe new file; one-file cost. Dissent recorded |
| 28 | DX 1 | no --update path; --yes regenerates silently | ADOPTED — --update preserves operator fields (REQ-201) |
| 29 | DX 2 | --add-environment single-row flow | DEFERRED — --update + --answers covers v1; revisit on operator friction |
| 30 | DX 3 | reset path undocumented | ADOPTED — --force documented (REQ-203) |
| 31 | DX 4-7 | refusals name what not fix | ADOPTED — remedy-text mandate (REQ-104/201) |
| 32 | DX 8-9 | decline scope/revocation undefined; stale declines suppress new evidence | ADOPTED — decline keyed (heuristic, evidence value); --reset-declines (REQ-203) |
| 33 | DX 10 | three question blocks undefined | ADOPTED — blocks mirror registry structure; surfaces isolated as own gate (REQ-201) |

## Risks (from design, kept live)

1. Parser-surface widening (registry = security input) → only `surfaces:` is
   machine-parsed, by the EXISTING hardened parser; adversarial fixtures.
2. Secret leakage into a committed file (highest consequence) → `--check`
   value-shape scan wired into FFS CI; detect never reads .env contents.
3. Clobbering consumer workflows → structural prefix + never-overwrite.
4. Advisory fatigue → one stderr line, prod path only.
5. Tier misclassification → proposal-only + unclassified-suite check failure.
6. verified-date rot → advisory only, never a gate.
7. Monorepo false positives → --detect-only review + per-row evidence.
