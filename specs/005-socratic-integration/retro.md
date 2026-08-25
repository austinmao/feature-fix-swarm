# Retro — spec 005 (socratic integration)

Date: 2026-08-06 · Run: spec-005 · Merged: PR #89 → `d47be17`

## Decision 1 — vendor-with-pin vs copy-once

**Decision: vendor-with-pin stands** (pin `862b52e` in `vendor/socratic/pin.json`,
materialized by `scripts/install-socratic.sh`, never committed as tree).

Context: the CEO-challenge round during planning argued copy-once (snapshot the
15 question files + 10 packs into the repo, drop the installer). Held against it
per the original operator direction ("could include using the repo itself and
having that as a dependency"), and the run's evidence supports keeping it:

- The pin gives provenance + a one-line upgrade path (`pin.json` bump → CI
  re-verifies enum parity) that a silent snapshot loses.
- The fail-soft consumption contract means an absent vendor tree costs nothing —
  copy-once's main benefit (always-present content) buys little.
- Enum freeze risk (upstream drift vs `DOMAIN_ENUM_ORDER`/`PACK_ENUM`) is now
  covered by the opt-in drift test added in the post-merge hygiene PR, replacing
  runbook step 5 as the only control.

Revisit trigger: upstream goes dead or the pin needs a fork-level patch — then
copy-once (or fork+pin) wins and this note is the pointer.

## Decision 2 — AC-011 findings-diff A/B never exercised

AC-011's armed-vs-unarmed reviewer A/B shipped as a **prompt-delta measurement
only** (+7,244 B / +15.58% armed prompt, `evals/socratic-ab.md`) against a stub
reviewer; a real findings-diff (does arming change what reviewers actually
find?) was never run. Recorded in-spec as an adjudicated deviation.

**Recommendation: don't run the full A/B now.** Cost is high (two full
review-gate FULL-tier passes over a controlled diff corpus, cross-model), and
the integration is advisory-by-design — no gate verdict depends on the armed
slice. Instead, harvest it passively: next 2–3 armed spec runs, note in the run
report whether the honest-verifier's socratic verify seam surfaced findings the
defect passes missed. If after ~3 runs it never moves a verdict, consider
un-arming the review-gate consumer (the un-arm trigger AC-011 left open);
if it does move one, that IS the A/B evidence, free.

## Follow-ups drained

The 5 open findings-queue items from the run (installer seam parity + dedup,
`legacy_skill_names` socratic, enum-drift opt-in test, prompt-master `--dest`
guard) are drained by the hygiene PR that carries this note. The 6th listed in
the handoff (HV observability) was already fixed in-run (`d6f3bd4`).

Open process debt (not this PR): plan-wall claude-rung schema validator defect
(rc=97) — forced every WAIVED verdict this run; tracked separately.
