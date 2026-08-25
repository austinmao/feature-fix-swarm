# Prior art — spec 007 (env registry + /ffs-init + CI scaffolding + test tiers)

Searched 2026-08-07: gh repo search ("environment registry yaml", "github
actions workflow templates hardened", "detect deployment environments cli",
"environments.yml staging production", "detect vercel.json wrangler.toml",
"test tiers smoke integration e2e config"), gh code search
(`staging_instance`), local skill scan, `compiler.engine.cli skill find`.

| candidate | type | stars/downloads | applicability verdict | evidence link |
|---|---|---|---|---|
| step-security/secure-repo | repo | 329 | PARTIAL — SHA-pins actions, min token permissions, adds scorecard/CodeQL workflows; rewrites EXISTING workflows (opposite of our anti-clobber rule); no registry, no tiers, no OIDC scaffolds | https://github.com/step-security/secure-repo |
| step-security/harden-runner | repo | 1242 | REJECT — runtime egress/process monitoring for runners, not template generation | https://github.com/step-security/harden-runner |
| step-security/github-actions-goat | repo | 514 | REJECT — deliberately-vulnerable training repo | https://github.com/step-security/github-actions-goat |
| actionlint / zizmor | repo | high | REJECT (known context) — workflow LINTERS; no generation, no registry | — |
| review-tier.sh (local) | script | n/a | REUSE — argv/env-override idiom for test-tier.sh | scripts/gsd/review-tier.sh:1-30 |
| `_parse_parity_manifest_yaml` (local) | code | n/a | REUSE — existing hardened dependency-free parser; registry designed to parse under it UNCHANGED | lib/gates.py (parser block; line refs re-verified in plan research) |
| templates/gsd-config.base.json (local) | template | n/a | CONVENTION — template dir exists; CI templates net-new | templates/ |

## Decision input

- Volume scout: no conflicting local skill/scaffold; review-tier.sh +
  parity parser are direct reuse anchors; testing-policy skill should be
  checked for tier-name alignment during planning.
- Execution researcher: zero registry-shaped OSS prior art above threshold;
  the registry+detection+tiers composite appears novel. Only overlap is
  secure-repo's SHA-pinning slice.

## Adjudication (judgment-tier judge, 2026-08-07)

Verdict: **build-fresh**; secure-repo is reference-only (fit ~5% — its one
behavior, rewriting existing workflows, is exactly what REQ-302 forbids;
delivery shape is a hosted app, nothing importable; license unverified — do
not copy template text verbatim). Borrow two authoring conventions by hand:
SHA-pin every `uses:` (+ version comment) and explicit least-privilege
`permissions:`. Pin maintenance = Dependabot, not new tooling. Judge's
adopted improvement: render also emits a `.github/dependabot.yml`
`github-actions` ecosystem entry (new file — REQ-302-safe) or flags its
absence in the proposal, so hand-copied pins don't rot silently in consumer
repos. Local reuse (parity parser, review-tier idiom) is where the real
adoption happens.
