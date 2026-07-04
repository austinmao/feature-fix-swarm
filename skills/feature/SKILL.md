---
name: feature
description: "DEPRECATED (v3.19.0) — replaced by /feature-spec (spec→plan→tasks→preflight→grant) + /feature-implement --autonomous (swarm impl→QA→review-gate→ship→canary). This stub chains the replacements for back-compat and will be removed next release."
version: "3.0.0-deprecated"
allowed-tools:
  - Read
  - Bash
  - Skill
---

# /feature — DEPRECATED

> **v3.19.0: `/feature` is retired.** Its post-decompose tail (review-gate → ship
> → canary) moved INTO `/feature-implement` as the Step 10 finish tail, gated by
> the autonomy ledger. Its front half is `/feature-spec` (which now also
> decomposes, preflights, and builds the grant ledger by default).

## New flow (use this)

```
/feature-spec NNN                      # spec → plan → clarify → tasks.md → preflight PASS → grant ledger
/feature-implement NNN --autonomous    # swarm implement → QA → review-gate → ship → canary (ledger-gated)
```

One operator stop total: the grant screen at the end of `/feature-spec`.

## Back-compat behavior (this release only)

When invoked, print the deprecation banner above, then chain the replacements:

1. If `specs/NNN/tasks.md` is missing → invoke the `feature-spec` skill with
   `$ARGUMENTS` (it ends at the grant screen — the operator is present by
   definition when calling /feature interactively).
2. Then invoke the `feature-implement` skill with `NNN --auto` (attended mode —
   the operator invoked /feature, so gates prompt normally; pass `--autonomous`
   yourself if a fresh preflight + grants exist).

Flags are NOT translated (`--resume`, `--no-canary`, `--skip-review-gate`,
`--no-goal` etc. from v2.5.0 are gone). If the caller passed any, print:
`[feature] flag <f> retired — see /feature-implement --help equivalents
(--no-finish skips ship/canary; --no-qa-loop skips QA loop)`.

## Why retired

- The pipeline's value was sequencing; sequencing now lives in the two surviving
  skills with a machine-checkable seam between them (preflight PASS + grant
  ledger + `gates.py analyze`/`agents_manifest.py check` on tasks.md).
- The `/goal` paste-banner workaround (v2.1.1) is obsolete: `--autonomous` +
  the ledger replaces goal-loop babysitting.
- Removal target: v3.20.0. Update anything that references `/feature` to the
  two-command flow.
